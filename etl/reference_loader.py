"""
Load reference data that is easier to build in Python than in T-SQL:

- staging_bidding_zone: the bidding zones from zones.py (source of dim_bidding_zone)
- ref_public_holiday:   public holidays per country and date (Python 'holidays' package, decision H3)

Usage (SSIS task "Load Reference Data"):
    python reference_loader.py <start_date> <end_date>

Holidays are loaded for every year of [start_date, end_date) (end exclusive, like the extract scripts);
existing rows are kept, missing ones added.
Tables are defined in database/create_warehouse.sql.
"""

import logging
import os
import sys
from datetime import date, timedelta

import holidays
import pyodbc

import zones
from etl_log import EtlStep

DEFAULT_CONNECTION_STRING = ("Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=EnergyWeatherDW;"
                             "Trusted_Connection=yes;TrustServerCertificate=yes;")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("reference_loader")


def load_bidding_zones(cursor) -> int:
    rows = [(code, info['name'], info['country'], zones.COUNTRY_NAMES[info['country']], info['timezone'])
            for code, info in zones.BIDDING_ZONES.items()]
    cursor.execute("TRUNCATE TABLE dbo.staging_bidding_zone")
    cursor.executemany("INSERT INTO dbo.staging_bidding_zone "
                       "(bidding_zone_code, bidding_zone_name, country_code, country_name, timezone) VALUES (?, ?, ?, ?, ?)",
                       rows)
    return len(rows)


def country_holidays(country: str, years: range) -> dict:
    """Holidays of one country in English where the package has a translation, else in the local language."""
    try:
        return holidays.country_holidays(country, years=years, language='en_US')
    except (NotImplementedError, KeyError, ValueError):
        return holidays.country_holidays(country, years=years)


def load_public_holidays(cursor, first_year: int, last_year: int) -> int:
    years = range(first_year, last_year + 1)
    rows = []
    for country in zones.EU_COUNTRIES:
        for day, name in sorted(country_holidays(country, years).items()):
            rows.append((country, day, name[:200]))

    # insert only the (country, date) pairs that are missing; existing rows are never changed
    cursor.execute("CREATE TABLE #holiday (country_code NVARCHAR(5), holiday_date DATE, holiday_name NVARCHAR(200))")
    cursor.executemany("INSERT INTO #holiday VALUES (?, ?, ?)", rows)
    cursor.execute("""
        INSERT INTO dbo.ref_public_holiday (country_code, holiday_date, holiday_name)
        SELECT h.country_code, h.holiday_date, h.holiday_name
        FROM #holiday h
        WHERE NOT EXISTS (SELECT 1 FROM dbo.ref_public_holiday r
                          WHERE r.country_code = h.country_code AND r.holiday_date = h.holiday_date)
    """)
    inserted = cursor.rowcount
    cursor.execute("DROP TABLE #holiday")
    logger.info(f"Holidays {first_year}-{last_year}: {len(rows)} found, {inserted} new")
    return inserted


def main():
    if len(sys.argv) < 3:
        print("Usage: python reference_loader.py <start_date> <end_date>")
        sys.exit(1)
    start = date.fromisoformat(sys.argv[1].strip('"')[:10])
    end = date.fromisoformat(sys.argv[2].strip('"')[:10])   # exclusive, like the extract scripts
    last_day = end - timedelta(days=1) if end > start else end

    connection_string = os.getenv('DW_CONNECTION_STRING') or DEFAULT_CONNECTION_STRING
    try:
        with EtlStep('REFERENCE_DATA', connection_string) as step:
            with pyodbc.connect(connection_string) as conn:
                cursor = conn.cursor()
                cursor.fast_executemany = True
                zones_loaded = load_bidding_zones(cursor)
                holidays_added = load_public_holidays(cursor, start.year, last_day.year)
                conn.commit()
            step.records = zones_loaded + holidays_added
            step.details = f"staging_bidding_zone: {zones_loaded} zones; ref_public_holiday: +{holidays_added} holidays"
            logger.info(f"Reference data loaded: {zones_loaded} bidding zones, {holidays_added} new holidays")
    except Exception as e:
        logger.error(f"Reference data load failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
