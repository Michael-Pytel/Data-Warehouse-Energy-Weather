"""
ENTSOEClient.py
Klient API do komunikacji z ENTSO-E Transparency Platform
Pobiera dane o zużyciu energii i generacji wg typów źródeł
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import xml.etree.ElementTree as ET
import pandas as pd
import zones
from etl_log import EtlStep
from datetime import datetime, timedelta
import time
import logging
from typing import Dict, List, Optional, Tuple
import pyodbc
import os
import sys
import threading
from collections import deque

# Requests are split into chunks of CHUNK_MONTHS calendar months (ENTSO-E allows at most 1 year per request).
# Measured 2026-11-17: a request takes ~14 s on the server whether it covers 1 or 3 months, so fewer, longer
# chunks are faster; 3 months keeps the largest response (DE generation) around 20 MB of XML.
CHUNK_MONTHS = int(os.getenv('ENTSOE_CHUNK_MONTHS', '3'))
# Parallel requests and request rate. ENTSO-E allows 400 requests/minute per token; exceeding it blocks
# the token for 10 minutes, so we stay well below (12 workers x ~14 s per request = ~50 requests/minute).
MAX_WORKERS = max(1, int(os.getenv('ENTSOE_MAX_WORKERS', '12')))
MAX_REQUESTS_PER_MINUTE = int(os.getenv('ENTSOE_MAX_REQUESTS_PER_MINUTE', '300'))
REQUEST_TIMEOUT = 300   # seconds; one year of DE generation took ~40 s
MAX_RETRIES = 5


class RateLimiter:
    """Allow at most `per_minute` calls in any 60-second window (thread-safe)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.calls = deque()
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                if len(self.calls) < self.per_minute:
                    self.calls.append(now)
                    return
                delay = 60 - (now - self.calls[0])
            time.sleep(delay)


class SourceError(RuntimeError):
    """ENTSO-E fails to serve a period ("Unexpected ERROR occurred during processing of use case").
    Seen 2026-11-17 for Cyprus actual load on 2025-10-09 only: every request that includes that day fails,
    the neighbouring days work. Retrying does not help; the chunk is split until the bad day is isolated."""


class ENTSOEClient:
    """Klient API dla ENTSO-E Transparency Platform"""
    
    def __init__(self, security_token: str, connection_string: str):
        """
        Inicjalizacja klienta ENTSO-E
        
        Args:
            security_token: Token API dla ENTSO-E
            connection_string: String połączenia z bazą danych
        """
        self.security_token = security_token
        self.base_url = "https://web-api.tp.entsoe.eu/api"
        self.connection_string = connection_string
        
        # Konfiguracja logowania
        logging.basicConfig(level=logging.INFO, 
                           format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)
        
        # Logowanie parametrów (bez wrażliwych danych)
        self.logger.info(f"Initializing ENTSOEClient with connection to {self.base_url}")
        
        # Mapowanie stref przetargowych
        self.bidding_zones = zones.bidding_zones()
        
        self.rate_limiter = RateLimiter(MAX_REQUESTS_PER_MINUTE)

    def _make_request(self, params: Dict) -> Optional[str]:
        """
        GET with rate limit and retries.
        Returns the XML, or None when ENTSO-E answers "No matching data found" (HTTP 200 acknowledgement).
        Raises RuntimeError when the request still fails after MAX_RETRIES attempts.
        """
        last_error = None
        for attempt in range(MAX_RETRIES):
            self.rate_limiter.wait()
            try:
                response = requests.get(self.base_url, params=params, timeout=REQUEST_TIMEOUT)
                body = response.text
                if 'Acknowledgement_MarketDocument' in body:
                    reason = ' '.join(re.findall(r'<text>(.*?)</text>', body)) or body[:300]
                    if response.status_code == 200 and 'No matching data found' in reason:
                        return None
                    last_error = f"HTTP {response.status_code}: {reason}"
                    if response.status_code == 400 and 'unexpected error' in reason.lower():
                        raise SourceError(last_error)
                    if response.status_code == 400:
                        break   # invalid request (e.g. period too long): retrying does not help
                elif response.status_code == 200:
                    return body
                else:
                    last_error = f"HTTP {response.status_code}: {body[:300]}"
                wait = 60 if response.status_code == 429 else min(60, 5 * 2 ** attempt)
            except requests.exceptions.RequestException as e:
                last_error = str(e)
                wait = min(60, 5 * 2 ** attempt)
            if attempt < MAX_RETRIES - 1:
                self.logger.warning(f"Request failed (attempt {attempt + 1}/{MAX_RETRIES}), retrying in {wait} s: {last_error}")
                time.sleep(wait)
        raise RuntimeError(last_error)

    @staticmethod
    def _resolution_to_timedelta(resolution: str) -> timedelta:
        """Convert an ISO 8601 resolution such as PT15M, PT60M or PT1H to a timedelta."""
        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", (resolution or "").strip())
        if not match or not any(match.groups()):
            return timedelta(hours=1)
        hours, minutes = (int(g) if g else 0 for g in match.groups())
        return timedelta(hours=hours, minutes=minutes)

    @staticmethod
    def _aggregate_to_hourly(records: List[Dict]) -> List[Dict]:
        """
        Average sub-hourly points (e.g. 15-minute MW values) to hourly values.
        The warehouse works at hourly grain; for power in MW the hourly mean is the correct value.
        """
        if not records:
            return records
        buckets: Dict[tuple, List[float]] = {}
        meta: Dict[tuple, Dict] = {}
        for record in records:
            hour = record['timestamp'].replace(minute=0, second=0, microsecond=0)
            key = (hour, record.get('bidding_zone'), record.get('generation_type'))
            buckets.setdefault(key, []).append(record['quantity'])
            meta.setdefault(key, {k: v for k, v in record.items() if k not in ('timestamp', 'quantity')})
        return [dict(meta[key], timestamp=key[0], quantity=sum(values) / len(values))
                for key, values in sorted(buckets.items(), key=lambda kv: kv[0][0])]

    def _parse_xml_response(self, xml_content: str) -> List[Dict]:
        """
        Parsowanie odpowiedzi XML do listy słowników
        
        Args:
            xml_content: Zawartość XML
            
        Returns:
            Lista słowników z danymi
        """
        if not xml_content:
            self.logger.error("Empty XML content provided to _parse_xml_response")
            return []
            
        try:
            self.logger.debug(f"Parsing XML response of length {len(xml_content)}")
            root = ET.fromstring(xml_content)
            
            # Wykryj przestrzeń nazw
            namespace = ''
            if '}' in root.tag:
                namespace = root.tag.split('}')[0] + '}'
                self.logger.debug(f"Detected namespace: {namespace}")
                
                # Utwórz mapowanie przestrzeni nazw dla findall
                namespaces = {'ns': namespace[1:-1]}  # Usuń znaki { i }
                self.logger.debug(f"Using namespace mapping: {namespaces}")
            else:
                self.logger.debug("No namespace detected in XML")
            
            data = []
            
            # Szukaj elementów TimeSeries z uwzględnieniem przestrzeni nazw
            if namespace:
                # Używamy xpath z przestrzenią nazw
                timeseries_elements = root.findall(".//ns:TimeSeries", namespaces)
                self.logger.debug(f"Found {len(timeseries_elements)} TimeSeries elements using namespace")
            else:
                # Próba bez przestrzeni nazw
                timeseries_elements = root.findall(".//TimeSeries")
                self.logger.debug(f"Found {len(timeseries_elements)} TimeSeries elements without namespace")
            
            # Jeśli nadal nie znaleziono, spróbuj inaczej
            if not timeseries_elements:
                # Ostatnia próba - przeszukaj wszystkie potomne elementy
                all_elements = list(root.iter())
                timeseries_elements = [elem for elem in all_elements if elem.tag.endswith('TimeSeries')]
                self.logger.debug(f"Found {len(timeseries_elements)} TimeSeries elements using iter method")
            
            if not timeseries_elements:
                self.logger.warning("No TimeSeries elements found in XML response")
                return []
            
            # Przetwarzanie każdego elementu TimeSeries
            for ts_idx, timeseries in enumerate(timeseries_elements):
                # Pobierz informacje o strefie przetargowej
                if namespace:
                    bidding_zone_elem = timeseries.find(".//ns:outBiddingZone_Domain.mRID", namespaces)
                    if bidding_zone_elem is None:
                        bidding_zone_elem = timeseries.find(".//ns:in_Domain.mRID", namespaces)
                else:
                    bidding_zone_elem = timeseries.find(".//outBiddingZone_Domain.mRID")
                    if bidding_zone_elem is None:
                        bidding_zone_elem = timeseries.find(".//in_Domain.mRID")
                
                bidding_zone = bidding_zone_elem.text if bidding_zone_elem is not None else None
                self.logger.debug(f"TimeSeries {ts_idx} - Bidding zone: {bidding_zone}")
                
                # Pobierz informacje o typie generacji (jeśli dostępne)
                generation_type = None
                if namespace:
                    psr_type_elem = timeseries.find(".//ns:MktPSRType/ns:psrType", namespaces)
                else:
                    psr_type_elem = timeseries.find(".//MktPSRType/psrType")
                
                if psr_type_elem is not None:
                    generation_type = psr_type_elem.text
                    self.logger.debug(f"TimeSeries {ts_idx} - Generation type: {generation_type}")

                    # Generation documents carry two series per type: production (inBiddingZone_Domain)
                    # and consumption, e.g. pumped-storage pumping (outBiddingZone_Domain).
                    # Only production belongs in the generation table.
                    in_zone = (timeseries.find(".//ns:inBiddingZone_Domain.mRID", namespaces) if namespace
                               else timeseries.find(".//inBiddingZone_Domain.mRID"))
                    if in_zone is None and bidding_zone_elem is not None:
                        self.logger.debug(f"TimeSeries {ts_idx} - skipping consumption series for {generation_type}")
                        continue
                    if in_zone is not None:
                        bidding_zone = in_zone.text

                # Pobierz okres czasu
                if namespace:
                    period_elements = timeseries.findall(".//ns:Period", namespaces)
                else:
                    period_elements = timeseries.findall(".//Period")
                
                self.logger.debug(f"TimeSeries {ts_idx} has {len(period_elements)} Period elements")
                
                # Przetwarzanie każdego okresu
                for period_idx, period in enumerate(period_elements):
                    # Pobierz interwał czasu
                    if namespace:
                        start_time_elem = period.find(".//ns:start", namespaces)
                    else:
                        start_time_elem = period.find(".//start")
                    
                    if start_time_elem is None:
                        self.logger.warning(f"No start time found for Period {period_idx} in TimeSeries {ts_idx}")
                        continue
                    
                    start_time_text = start_time_elem.text
                    self.logger.debug(f"Period {period_idx} - Start time: {start_time_text}")
                    
                    # Convert UTC start time to datetime
                    start_datetime = datetime.fromisoformat(start_time_text.replace('Z', '+00:00'))

                    # Resolution of this period (PT15M, PT30M, PT60M, ...); ENTSO-E mixes them by country
                    find = (lambda el, tag: el.find(f".//ns:{tag}", namespaces)) if namespace else (lambda el, tag: el.find(f".//{tag}"))
                    resolution_elem = find(period, "resolution")
                    step = self._resolution_to_timedelta(resolution_elem.text if resolution_elem is not None else "PT60M")
                    end_time_elem = find(period, "end")
                    end_datetime = (datetime.fromisoformat(end_time_elem.text.replace('Z', '+00:00'))
                                    if end_time_elem is not None else None)
                    curve_elem = find(timeseries, "curveType")
                    curve_type = curve_elem.text if curve_elem is not None else "A01"

                    # Collect points
                    if namespace:
                        point_elements = period.findall(".//ns:Point", namespaces)
                    else:
                        point_elements = period.findall(".//Point")

                    self.logger.debug(f"Period {period_idx} has {len(point_elements)} Point elements, resolution {step}")

                    points = {}
                    for point in point_elements:
                        position_elem = find(point, "position")
                        quantity_elem = find(point, "quantity")
                        if position_elem is not None and quantity_elem is not None:
                            points[int(position_elem.text)] = float(quantity_elem.text)

                    if not points:
                        continue

                    # A03 curves omit positions whose value repeats the previous one: fill them forward
                    if curve_type == "A03" and end_datetime is not None:
                        n_positions = int((end_datetime - start_datetime) / step)
                        last = None
                        for pos in range(1, n_positions + 1):
                            if pos in points:
                                last = points[pos]
                            elif last is not None:
                                points[pos] = last

                    for position, quantity in sorted(points.items()):
                        record = {
                            'timestamp': start_datetime + step * (position - 1),
                            'quantity': quantity
                        }
                        if bidding_zone:
                            record['bidding_zone'] = bidding_zone
                        if generation_type:
                            record['generation_type'] = generation_type
                        data.append(record)

            data = self._aggregate_to_hourly(data)
            self.logger.debug(f"Parsed {len(data)} hourly data points from XML")
            return data
            
        except ET.ParseError as e:
            raise RuntimeError(f"XML parsing error: {e}") from e
    
    # data type -> request parameters and staging table (tables defined in database/create_warehouse.sql)
    DATA_TYPES = {
        'actual_load': {'documentType': 'A65', 'processType': 'A16', 'zone_param': 'outBiddingZone_Domain',
                        'table': 'staging_entso_actual_load'},
        'generation':  {'documentType': 'A75', 'processType': 'A16', 'zone_param': 'in_Domain',
                        'table': 'staging_entso_generation'},
        'forecast':    {'documentType': 'A65', 'processType': 'A01', 'zone_param': 'outBiddingZone_Domain',
                        'table': 'staging_entso_forecast'},
    }

    def fetch(self, zone_code: str, data_type: str, start: datetime, end: datetime) -> Tuple[pd.DataFrame, List[str]]:
        """
        Like fetch_chunk, but when ENTSO-E cannot serve the period (SourceError) the chunk is split in halves
        down to single days. Returns the data and the days ENTSO-E could not serve ("source gaps").
        """
        try:
            return self.fetch_chunk(zone_code, data_type, start, end), []
        except SourceError:
            days = (end - start).days
            if days <= 1:
                self.logger.warning(f"ENTSO-E cannot serve {zone_code} {data_type} {start:%Y-%m-%d} (source error)")
                return pd.DataFrame(), [f"{start:%Y-%m-%d}"]
            middle = start + timedelta(days=days // 2)
            first, first_gaps = self.fetch(zone_code, data_type, start, middle)
            second, second_gaps = self.fetch(zone_code, data_type, middle, end)
            parts = [df for df in (first, second) if not df.empty]
            return (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()), first_gaps + second_gaps

    def fetch_chunk(self, zone_code: str, data_type: str, start: datetime, end: datetime) -> pd.DataFrame:
        """
        Hourly values of one data type for one zone and one chunk [start, end) in UTC.
        Empty DataFrame when ENTSO-E has no data; raises when the request fails.
        """
        spec = self.DATA_TYPES[data_type]
        params = {
            'securityToken': self.security_token,
            'documentType': spec['documentType'],
            'processType': spec['processType'],
            spec['zone_param']: zone_code,
            'periodStart': start.strftime('%Y%m%d%H00'),
            'periodEnd': end.strftime('%Y%m%d%H00'),
        }
        xml_content = self._make_request(params)
        if xml_content is None:
            return pd.DataFrame()
        records = self._parse_xml_response(xml_content)
        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        # UTC, without time zone for DATETIME2; keep only the requested chunk (no overlap between chunks)
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True).dt.tz_localize(None)
        df = df[(df['timestamp'] >= start) & (df['timestamp'] < end)]
        zone_info = self.bidding_zones[zone_code]
        return df.assign(zone_code=zone_code, zone_name=zone_info['name'], country=zone_info['country'],
                         data_type=data_type)

    @staticmethod
    def month_chunks(start: datetime, end: datetime, months: int = 1) -> List[Tuple[datetime, datetime]]:
        """Split [start, end) at calendar-month boundaries into chunks of `months` months (1-12; ENTSO-E: max 1 year)."""
        months = min(max(months, 1), 12)
        chunks, chunk_start = [], start
        while chunk_start < end:
            month_index = chunk_start.year * 12 + chunk_start.month - 1 + months
            boundary = datetime(month_index // 12, month_index % 12 + 1, 1)
            chunk_end = min(boundary, end)
            chunks.append((chunk_start, chunk_end))
            chunk_start = chunk_end
        return chunks

    def extract_to_staging(self, start: datetime, end: datetime) -> Dict:
        """
        Fetch all zones and data types for [start, end) in chunks of CHUNK_MONTHS months (parallel, rate-limited)
        and write each chunk to staging as soon as it arrives. The staging tables are emptied first.

        Returns {'rows': {data_type: n}, 'no_data': [...], 'failed': [...], 'source_gaps': [...]}
        'failed' (network errors, timeouts after all retries) makes the step fail; 'source_gaps' (single
        days ENTSO-E cannot serve) are logged but do not, because a rerun cannot fix them.
        """
        chunks = self.month_chunks(start, end, CHUNK_MONTHS)
        tasks = [(zone_code, data_type, chunk_start, chunk_end)
                 for zone_code in self.bidding_zones
                 for data_type in self.DATA_TYPES
                 for chunk_start, chunk_end in chunks]
        self.logger.info(f"ENTSO-E {start:%Y-%m-%d} -> {end:%Y-%m-%d} (end exclusive): {len(self.bidding_zones)} zones x "
                         f"{len(self.DATA_TYPES)} data types x {len(chunks)} chunks = {len(tasks)} requests, "
                         f"{MAX_WORKERS} parallel, max {MAX_REQUESTS_PER_MINUTE}/min")

        result = {'rows': {data_type: 0 for data_type in self.DATA_TYPES}, 'no_data': [], 'failed': [],
                  'source_gaps': []}
        conn = pyodbc.connect(self.connection_string)
        try:
            cursor = conn.cursor()
            for spec in self.DATA_TYPES.values():
                if cursor.execute("SELECT OBJECT_ID(?, 'U')", f"dbo.{spec['table']}").fetchone()[0] is None:
                    raise RuntimeError(f"{spec['table']} does not exist - run database/create_warehouse.sql first")
                cursor.execute(f"TRUNCATE TABLE dbo.{spec['table']}")
            conn.commit()
            cursor.fast_executemany = True

            started = time.time()
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {pool.submit(self.fetch, *task): task for task in tasks}
                for done, future in enumerate(as_completed(futures), 1):
                    zone_code, data_type, chunk_start, chunk_end = futures[future]
                    label = f"{self.bidding_zones[zone_code]['name']} {data_type} {chunk_start:%Y-%m-%d}"
                    try:
                        df, gaps = future.result()
                    except Exception as e:
                        self.logger.error(f"FAILED {label}: {e}")
                        result['failed'].append(f"{label}: {e}")
                        continue
                    if gaps:
                        result['source_gaps'].append(
                            f"{self.bidding_zones[zone_code]['name']} {data_type} {', '.join(gaps)}")
                    if df.empty:
                        result['no_data'].append(label)
                    else:
                        self._insert(cursor, self.DATA_TYPES[data_type]['table'], df)
                        conn.commit()
                        result['rows'][data_type] += len(df)
                    if done % 50 == 0 or done == len(tasks):
                        elapsed = time.time() - started
                        self.logger.info(f"{done}/{len(tasks)} requests done ({elapsed / 60:.1f} min, "
                                         f"~{elapsed / done * (len(tasks) - done) / 60:.0f} min left), "
                                         f"rows so far: {result['rows']}")
        finally:
            conn.close()
        return result

    @staticmethod
    def _insert(cursor, table: str, df: pd.DataFrame):
        columns = ['timestamp', 'quantity', 'zone_code', 'zone_name', 'country', 'data_type']
        if table == 'staging_entso_generation':
            columns.append('generation_type')
        rows = [(ts.to_pydatetime(), *rest) for ts, *rest in df[columns].itertuples(index=False, name=None)]
        cursor.executemany(f"INSERT INTO dbo.{table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                           rows)

# Funkcja główna do uruchomienia z SSIS
def main():
    """Główna funkcja wywoływana przez SSIS"""
    # Konfiguracja logowania
    import sys
    import os
    import argparse
    from datetime import datetime, timedelta
    
    logging.basicConfig(level=logging.INFO,
                       format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger("ENTSOEMain")
    
    logger.info("Starting ENTSOEClient process")
    
    # Ręczne parsowanie argumentów (bez argparse)
    token = None
    connection_string = None
    start_date_str = None
    end_date_str = None
    
    # Parsowanie argumentów ręcznie
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i].startswith('--token'):
            if '=' in args[i]:
                token = args[i].split('=', 1)[1]
            elif i + 1 < len(args):
                token = args[i + 1]
                i += 1
        elif args[i].startswith('--connection'):
            if '=' in args[i]:
                connection_string = args[i].split('=', 1)[1]
            elif i + 1 < len(args):
                connection_string = args[i + 1]
                i += 1
        elif args[i].startswith('--start'):
            if '=' in args[i]:
                start_date_str = args[i].split('=', 1)[1]
            elif i + 1 < len(args):
                start_date_str = args[i + 1]
                i += 1
        elif args[i].startswith('--end'):
            if '=' in args[i]:
                end_date_str = args[i].split('=', 1)[1]
            elif i + 1 < len(args):
                end_date_str = args[i + 1]
                i += 1
        i += 1
    
    # Jeśli nie podano argumentów, spróbuj użyć zmiennych środowiskowych
    if not token:
        token = os.getenv('ENTSO_TOKEN', '')
    
    if not connection_string:
        connection_string = os.getenv('DW_CONNECTION_STRING', '')

    if not connection_string:
        # Same default as the other ETL scripts
        connection_string = "Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=EnergyWeatherDW;Trusted_Connection=yes;TrustServerCertificate=yes;"
        logger.info("Using default local connection string")

    # Sprawdzenie czy wymagane argumenty są dostępne
    if not token:
        logger.error("ENTSO-E API token is missing. Provide --token argument or set ENTSO_TOKEN environment variable")
        sys.exit(1)
    
    if not connection_string:
        logger.error("Database connection string is missing. Provide --connection argument or set DW_CONNECTION_STRING environment variable")
        sys.exit(1)
        
    logger.info(f"Security token found: {'Yes' if token else 'No'}")
    logger.info(f"Connection string found: {'Yes' if connection_string else 'No'}")
    
    # Domyślnie pobieramy dane z ostatnich 24 godzin
    end_date = datetime.now().replace(minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(hours=24)
    
    # Przetwarzanie dat z argumentów
    if start_date_str:
        try:
            logger.info(f"Parsing start date: {start_date_str}")
            # Obsługa różnych formatów daty
            if 'T' in start_date_str:  # Format ISO z czasem
                start_date = datetime.fromisoformat(start_date_str)
            else:  # Tylko data
                start_date = datetime.fromisoformat(start_date_str + 'T00:00:00')
            logger.info(f"Using custom start date: {start_date}")
        except ValueError as e:
            logger.error(f"Invalid start date format: {e}")
            sys.exit(1)
            
    if end_date_str:
        try:
            logger.info(f"Parsing end date: {end_date_str}")
            # Obsługa różnych formatów daty
            if 'T' in end_date_str:  # Format ISO z czasem
                end_date = datetime.fromisoformat(end_date_str)
            else:  # Tylko data
                end_date = datetime.fromisoformat(end_date_str + 'T00:00:00')
            logger.info(f"Using custom end date: {end_date}")
        except ValueError as e:
            logger.error(f"Invalid end date format: {e}")
            sys.exit(1)
    
    logger.info(f"Data extraction period: {start_date} to {end_date}")

    with EtlStep('ENTSOE_EXTRACT', connection_string) as step:
        try:
            client = ENTSOEClient(token, connection_string)
            result = client.extract_to_staging(start_date, end_date)

            step.records = sum(result['rows'].values())
            details = [', '.join(f"{data_type}: {n}" for data_type, n in result['rows'].items())]
            if result['no_data']:
                details.append(f"no data for {len(result['no_data'])} zone/type/months, e.g. "
                               + '; '.join(result['no_data'][:5]))
            if result['source_gaps']:
                details.append("days ENTSO-E cannot serve (source error): " + '; '.join(result['source_gaps']))
            if result['failed']:
                details.append(f"FAILED {len(result['failed'])} requests: " + '; '.join(result['failed'][:5]))
            step.details = ' | '.join(details)
            logger.info(step.details)

            if result['failed']:
                logger.error(f"{len(result['failed'])} requests failed - staging is incomplete")
                sys.exit(1)
            logger.info("ENTSO-E data extraction completed successfully")
            sys.exit(0)

        except Exception as e:
            logger.error(f"Fatal error: {str(e)}", exc_info=True)
            sys.exit(1)

if __name__ == "__main__":
    main()