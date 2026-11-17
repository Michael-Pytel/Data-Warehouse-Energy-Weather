"""
Eurostat extract: yearly socio-economic indicators per EU country -> staging_eurostat_raw.

    python eurostat_client.py <since_year> ["AT,BE,..."]     (SSIS task "Execute Eurostat Data")

Downloads the five indicators of decision H1 from the Eurostat dissemination API (JSON-stat 2.0), replaces
the contents of staging_eurostat_raw and writes one CSV per indicator to eurostat_export/ (a readable copy
of what was loaded). The transformation into dim_socioeconomic_profile (per country and year, SCD Type 2,
estimates for years not published yet) is done in T-SQL (usp_load_dim_socioeconomic_profile).

Each query pins every dimension except country (geo) and period (time) to one value, so each
(country, period) has exactly one value. There are no fallbacks to other units or datasets: a query
that fails or returns no data fails the step instead of loading something different.
"""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import pyodbc
import requests

import zones
from etl_log import EtlStep, start_run_log

BASE_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
DEFAULT_CONNECTION_STRING = ("Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=EnergyWeatherDW;"
                             "Trusted_Connection=yes;TrustServerCertificate=yes;")
EXPORT_DIR = Path(__file__).resolve().parent / "eurostat_export"
REQUEST_TIMEOUT = 120
MAX_RETRIES = 4

# indicator -> Eurostat dataset and the filter that leaves one value per country and period
DATASETS = {
    'population': {
        'code': 'demo_pjan',                       # population on 1 January
        'params': {'sex': 'T', 'age': 'TOTAL'},
    },
    'gdp_per_capita': {
        'code': 'nama_10_pc',                      # GDP at market prices, current prices, EUR per inhabitant
        'params': {'unit': 'CP_EUR_HAB', 'na_item': 'B1GQ'},
    },
    'electricity_prices': {
        'code': 'nrg_pc_204',                      # household electricity price, half-yearly (2023-S1, 2023-S2)
        'params': {'unit': 'KWH', 'siec': 'E7000', 'nrg_cons': 'KWH2500-4999', 'tax': 'X_TAX', 'currency': 'EUR'},
    },
    'energy_intensity': {
        'code': 'sdg_07_30',                       # energy intensity of the economy
        'params': {'unit': 'EUR_KGOE'},
    },
    'unemployment_rate': {
        'code': 'une_rt_a',                        # unemployment rate, % of active population aged 15-74
        'params': {'sex': 'T', 'age': 'Y15-74', 'unit': 'PC_ACT'},
    },
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("eurostat_client")


def request_dataset(code: str, params: Dict) -> Dict:
    """GET one dataset as JSON-stat; retries network errors, 429 and 5xx. Raises when it still fails."""
    url = f"{BASE_URL}/{code}"
    params = dict(params, format='JSON', lang='EN')
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response.json()
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code < 500 and response.status_code != 429:
                break                                  # invalid query: retrying does not help
        except requests.exceptions.RequestException as e:
            last_error = str(e)
        if attempt < MAX_RETRIES - 1:
            wait = 5 * 2 ** attempt
            logger.warning(f"{code}: attempt {attempt + 1}/{MAX_RETRIES} failed, retrying in {wait} s: {last_error}")
            time.sleep(wait)
    raise RuntimeError(f"{code}: {last_error}")


def parse_jsonstat(data: Dict) -> pd.DataFrame:
    """
    JSON-stat 2.0 -> one row per (country, period) with a value.
    'value' is a flat array (or a sparse {position: value} object) in row-major order over the
    dimensions listed in 'id' with the sizes in 'size'.
    """
    ids, sizes, dims = data['id'], data['size'], data['dimension']
    extra = [(d, s) for d, s in zip(ids, sizes) if d not in ('geo', 'time') and s != 1]
    if extra:
        raise ValueError("the query must leave exactly one value for each dimension except geo and time; "
                         + ", ".join(f"{d} has {s}" for d, s in extra))

    # position -> code for every dimension ('index' maps code -> position)
    codes = {d: {pos: code for code, pos in dims[d]['category']['index'].items()} for d in ids}
    geo_labels = dims['geo']['category'].get('label', {})
    strides = [1] * len(ids)
    for i in range(len(ids) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    values = data.get('value') or {}
    items = values.items() if isinstance(values, dict) else enumerate(values)
    rows = []
    for flat, value in items:
        if value is None:
            continue
        flat = int(flat)
        position = {d: (flat // strides[i]) % sizes[i] for i, d in enumerate(ids)}
        geo = codes['geo'][position['geo']]
        rows.append({
            'country_code': zones.EUROSTAT_TO_ISO.get(geo, geo),    # Eurostat EL -> ISO GR
            'country_name': geo_labels.get(geo, geo),
            'time_period': codes['time'][position['time']],
            'value': float(value),
        })
    return pd.DataFrame(rows, columns=['country_code', 'country_name', 'time_period', 'value'])


def fetch_indicator(name: str, countries: List[str], since_year: int) -> pd.DataFrame:
    spec = DATASETS[name]
    params = dict(spec['params'], sinceTimePeriod=str(since_year),
                  geo=[zones.EUROSTAT_GEO_CODES.get(c, c) for c in countries])
    df = parse_jsonstat(request_dataset(spec['code'], params))
    df = df[df['country_code'].isin(countries)].sort_values(['country_code', 'time_period'], ignore_index=True)
    if df.empty:
        raise RuntimeError(f"{name} ({spec['code']}): no values returned")
    missing = sorted(set(countries) - set(df['country_code']))
    logger.info(f"{name} ({spec['code']}): {len(df)} values, {df['country_code'].nunique()} countries, "
                f"periods {df['time_period'].min()} - {df['time_period'].max()}"
                + (f", no data for {', '.join(missing)}" if missing else ""))
    return df


def export_to_csv(raw: Dict[str, pd.DataFrame]):
    """One readable CSV per indicator (overwritten on every run)."""
    EXPORT_DIR.mkdir(exist_ok=True)
    for name, df in raw.items():
        df.to_csv(EXPORT_DIR / f"eurostat_raw_{name}.csv", index=False, encoding='utf-8')
    logger.info(f"CSV copies written to {EXPORT_DIR}")


def save_to_staging(connection_string: str, raw: Dict[str, pd.DataFrame]) -> int:
    """Replace the contents of staging_eurostat_raw (table defined in database/create_warehouse.sql)."""
    rows = [(name, r.country_code, r.country_name, r.time_period, r.value)
            for name, df in raw.items() for r in df.itertuples(index=False)]
    with pyodbc.connect(connection_string) as conn:
        cursor = conn.cursor()
        if cursor.execute("SELECT OBJECT_ID('dbo.staging_eurostat_raw', 'U')").fetchone()[0] is None:
            raise RuntimeError("staging_eurostat_raw does not exist - run database/create_warehouse.sql first")
        cursor.execute("TRUNCATE TABLE dbo.staging_eurostat_raw")
        cursor.fast_executemany = True
        cursor.executemany("INSERT INTO dbo.staging_eurostat_raw (dataset_name, country_code, country_name, "
                           "time_period, value) VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
    logger.info(f"{len(rows)} rows written to staging_eurostat_raw")
    return len(rows)


def parse_args(argv: List[str]):
    since_year = int(argv[1]) if len(argv) > 1 else 2020
    countries: Optional[List[str]] = None
    if len(argv) > 2 and argv[2].strip('"').strip():
        countries = [c.strip().upper() for c in argv[2].strip('"').split(',') if c.strip()]
    countries = countries or list(zones.EU_COUNTRIES)
    unknown = sorted(set(countries) - set(zones.EU_COUNTRIES))
    if unknown:
        raise ValueError(f"unknown country codes: {', '.join(unknown)}")
    return since_year, countries


def main():
    start_run_log('eurostat_client')
    connection_string = os.getenv('DW_CONNECTION_STRING') or DEFAULT_CONNECTION_STRING
    try:
        since_year, countries = parse_args(sys.argv)
    except ValueError as e:
        logger.error(f"Usage: python eurostat_client.py <since_year> [\"AT,BE,...\"] - {e}")
        sys.exit(1)

    with EtlStep('EUROSTAT_EXTRACT', connection_string) as step:
        logger.info(f"Eurostat extract since {since_year} for {len(countries)} countries")
        raw = {name: fetch_indicator(name, countries, since_year) for name in DATASETS}
        export_to_csv(raw)
        step.records = save_to_staging(connection_string, raw)
        step.details = ', '.join(f"{name}: {len(df)}" for name, df in raw.items())
        logger.info(f"Eurostat extract completed: {step.details}")


if __name__ == "__main__":
    main()
