"""
Open-Meteo extract: hourly historical weather (archive API, UTC) for representative cities of all
27 EU countries -> staging_weather_data.

    python open_meteo_client.py <start_date> <end_date>      (SSIS task "Extract Weather Data")

The end date is exclusive. Requests are made per city and calendar year and paced to the free-tier
limits (see LIMITS). The country average per hour is computed in T-SQL (vw_weather_country_hour).
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import logging
from typing import Dict, List, Optional, Tuple
import pyodbc
import json
import os
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

from etl_log import EtlStep, start_run_log

# Open-Meteo free tier: 600 calls/minute, 5,000/hour, 10,000/day per IP address. A request counts as several
# calls when it covers more than 2 weeks or more than 10 variables (weight = days/14 x variables/10, at least 1).
# Requests are made per city and calendar year and paced below these limits; a full 3-year load
# (89 cities, ~7,000 weighted calls) therefore takes a little over an hour.
MAX_WORKERS = 4                          # more parallel requests get "429 Too many concurrent requests"
LIMITS = {60: 500, 3600: 4500}           # window in seconds -> max weighted calls (margin below 600 / 5,000)
DAILY_LIMIT = 10000
REQUEST_TIMEOUT = 120
MAX_RETRIES = 5


class WeightedRateLimiter:
    """Keep the weighted calls in every window (e.g. 60 s, 3600 s) below its limit (thread-safe)."""

    def __init__(self, limits: Dict[int, float]):
        self.limits = limits
        self.calls = deque()                 # (time, weight)
        self.lock = threading.Lock()

    def wait(self, weight: float, logger=None):
        while True:
            with self.lock:
                now = time.monotonic()
                longest = max(self.limits)
                while self.calls and now - self.calls[0][0] >= longest:
                    self.calls.popleft()
                delay = 0
                for window, limit in self.limits.items():
                    in_window = [(t, w) for t, w in self.calls if now - t < window]
                    used = sum(w for _, w in in_window)
                    if used + weight > limit:
                        # wait until enough of the oldest calls have left this window
                        for t, w in in_window:
                            used -= w
                            if used + weight <= limit:
                                delay = max(delay, window - (now - t) + 0.5)
                                break
                if delay == 0:
                    self.calls.append((now, weight))
                    return
            if logger and delay > 30:
                logger.info(f"Open-Meteo rate limit: waiting {delay / 60:.1f} min")
            time.sleep(delay)


# WMO weather interpretation codes (Open-Meteo `weather_code`) mapped to the condition types
# of dim_weather_condition (see database/create_warehouse.sql). Unlisted codes become 'Unknown'.
WMO_CONDITIONS = {
    0: 'Clear', 1: 'Clear',                     # clear sky, mainly clear
    2: 'Partly Cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Fog',                       # fog, depositing rime fog
    51: 'Rain', 53: 'Rain', 55: 'Rain',         # drizzle
    56: 'Rain', 57: 'Rain',                     # freezing drizzle
    61: 'Rain', 63: 'Rain', 65: 'Heavy Rain',   # rain: slight, moderate, heavy
    66: 'Rain', 67: 'Heavy Rain',               # freezing rain
    71: 'Snow', 73: 'Snow', 75: 'Heavy Snow', 77: 'Snow',   # snow fall, snow grains
    80: 'Rain', 81: 'Rain', 82: 'Heavy Rain',   # rain showers
    85: 'Snow', 86: 'Heavy Snow',               # snow showers
    95: 'Storm', 96: 'Storm', 99: 'Storm',      # thunderstorm (with hail)
}


class OpenMeteoClient:
    """Client for the Open-Meteo historical weather (archive) API."""
    
    def __init__(self, connection_string: str):
        """
        Args:
            connection_string: ODBC connection string of the warehouse
        """
        # Archive API = real historical weather. (The climate API is model simulation; not used.)
        self.base_url = "https://archive-api.open-meteo.com/v1/archive"
        self.connection_string = connection_string
        
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
        
        # representative cities per EU country; the country value is their average (decision A)
        self.weather_locations = {
            'AT': {  # Austria
                'name': 'Austria',
                'subzones': [
                    {'name': 'Vienna', 'lat': 48.2082, 'lon': 16.3738, 'subzone_code': 'AT-VIE'},
                    {'name': 'Salzburg', 'lat': 47.8095, 'lon': 13.0550, 'subzone_code': 'AT-SAL'},
                    {'name': 'Innsbruck', 'lat': 47.2692, 'lon': 11.4041, 'subzone_code': 'AT-INN'}
                ]
            },
            'BE': {  # Belgium
                'name': 'Belgium',
                'subzones': [
                    {'name': 'Brussels', 'lat': 50.8503, 'lon': 4.3517, 'subzone_code': 'BE-BRU'},
                    {'name': 'Antwerp', 'lat': 51.2194, 'lon': 4.4025, 'subzone_code': 'BE-ANT'},
                    {'name': 'Ghent', 'lat': 51.0543, 'lon': 3.7174, 'subzone_code': 'BE-GHE'}
                ]
            },
            'BG': {  # Bulgaria
                'name': 'Bulgaria',
                'subzones': [
                    {'name': 'Sofia', 'lat': 42.6977, 'lon': 23.3219, 'subzone_code': 'BG-SOF'},
                    {'name': 'Plovdiv', 'lat': 42.1354, 'lon': 24.7453, 'subzone_code': 'BG-PLO'},
                    {'name': 'Varna', 'lat': 43.2141, 'lon': 27.9147, 'subzone_code': 'BG-VAR'}
                ]
            },
            'HR': {  # Croatia
                'name': 'Croatia',
                'subzones': [
                    {'name': 'Zagreb', 'lat': 45.8150, 'lon': 15.9819, 'subzone_code': 'HR-ZAG'},
                    {'name': 'Split', 'lat': 43.5081, 'lon': 16.4402, 'subzone_code': 'HR-SPL'},
                    {'name': 'Rijeka', 'lat': 45.3271, 'lon': 14.4422, 'subzone_code': 'HR-RIJ'}
                ]
            },
            'CY': {  # Cyprus
                'name': 'Cyprus',
                'subzones': [
                    {'name': 'Nicosia', 'lat': 35.1856, 'lon': 33.3823, 'subzone_code': 'CY-NIC'},
                    {'name': 'Limassol', 'lat': 34.6751, 'lon': 33.0392, 'subzone_code': 'CY-LIM'},
                    {'name': 'Larnaca', 'lat': 34.9086, 'lon': 33.6290, 'subzone_code': 'CY-LAR'}
                ]
            },
            'CZ': {  # Czech Republic
                'name': 'Czech Republic',
                'subzones': [
                    {'name': 'Prague', 'lat': 50.0755, 'lon': 14.4378, 'subzone_code': 'CZ-PRG'},
                    {'name': 'Brno', 'lat': 49.1951, 'lon': 16.6068, 'subzone_code': 'CZ-BRN'},
                    {'name': 'Ostrava', 'lat': 49.8209, 'lon': 18.2625, 'subzone_code': 'CZ-OST'}
                ]
            },
            'DK': {  # Denmark
                'name': 'Denmark',
                'subzones': [
                    {'name': 'Copenhagen', 'lat': 55.6761, 'lon': 12.5683, 'subzone_code': 'DK-COP'},
                    {'name': 'Aarhus', 'lat': 56.1629, 'lon': 10.2039, 'subzone_code': 'DK-AAR'},
                    {'name': 'Aalborg', 'lat': 57.0488, 'lon': 9.9217, 'subzone_code': 'DK-AAL'}
                ]
            },
            'EE': {  # Estonia
                'name': 'Estonia',
                'subzones': [
                    {'name': 'Tallinn', 'lat': 59.4370, 'lon': 24.7536, 'subzone_code': 'EE-TAL'},
                    {'name': 'Tartu', 'lat': 58.3780, 'lon': 26.7290, 'subzone_code': 'EE-TAR'},
                    {'name': 'Narva', 'lat': 59.3772, 'lon': 28.1903, 'subzone_code': 'EE-NAR'}
                ]
            },
            'FI': {  # Finland
                'name': 'Finland',
                'subzones': [
                    {'name': 'Helsinki', 'lat': 60.1699, 'lon': 24.9384, 'subzone_code': 'FI-HEL'},
                    {'name': 'Tampere', 'lat': 61.4991, 'lon': 23.7871, 'subzone_code': 'FI-TAM'},
                    {'name': 'Turku', 'lat': 60.4518, 'lon': 22.2666, 'subzone_code': 'FI-TUR'}
                ]
            },
            'FR': {  # France
                'name': 'France',
                'subzones': [
                    {'name': 'Paris', 'lat': 48.8566, 'lon': 2.3522, 'subzone_code': 'FR-PAR'},
                    {'name': 'Lyon', 'lat': 45.7640, 'lon': 4.8357, 'subzone_code': 'FR-LYO'},
                    {'name': 'Marseille', 'lat': 43.2965, 'lon': 5.3698, 'subzone_code': 'FR-MAR'},
                    {'name': 'Toulouse', 'lat': 43.6047, 'lon': 1.4442, 'subzone_code': 'FR-TOU'}
                ]
            },
            'DE': {  # Germany
                'name': 'Germany',
                'subzones': [
                    {'name': 'Berlin', 'lat': 52.5200, 'lon': 13.4050, 'subzone_code': 'DE-BER'},
                    {'name': 'Munich', 'lat': 48.1351, 'lon': 11.5820, 'subzone_code': 'DE-MUN'},
                    {'name': 'Hamburg', 'lat': 53.5511, 'lon': 9.9937, 'subzone_code': 'DE-HAM'},
                    {'name': 'Frankfurt', 'lat': 50.1109, 'lon': 8.6821, 'subzone_code': 'DE-FRA'}
                ]
            },
            'GR': {  # Greece
                'name': 'Greece',
                'subzones': [
                    {'name': 'Athens', 'lat': 37.9838, 'lon': 23.7275, 'subzone_code': 'GR-ATH'},
                    {'name': 'Thessaloniki', 'lat': 40.6401, 'lon': 22.9444, 'subzone_code': 'GR-THE'},
                    {'name': 'Patras', 'lat': 38.2466, 'lon': 21.7346, 'subzone_code': 'GR-PAT'}
                ]
            },
            'HU': {  # Hungary
                'name': 'Hungary',
                'subzones': [
                    {'name': 'Budapest', 'lat': 47.4979, 'lon': 19.0402, 'subzone_code': 'HU-BUD'},
                    {'name': 'Debrecen', 'lat': 47.5316, 'lon': 21.6273, 'subzone_code': 'HU-DEB'},
                    {'name': 'Szeged', 'lat': 46.2530, 'lon': 20.1414, 'subzone_code': 'HU-SZE'}
                ]
            },
            'IE': {  # Ireland
                'name': 'Ireland',
                'subzones': [
                    {'name': 'Dublin', 'lat': 53.3498, 'lon': -6.2603, 'subzone_code': 'IE-DUB'},
                    {'name': 'Cork', 'lat': 51.8985, 'lon': -8.4756, 'subzone_code': 'IE-COR'},
                    {'name': 'Galway', 'lat': 53.2707, 'lon': -9.0568, 'subzone_code': 'IE-GAL'}
                ]
            },
            'IT': {  # Italy
                'name': 'Italy',
                'subzones': [
                    {'name': 'Rome', 'lat': 41.9028, 'lon': 12.4964, 'subzone_code': 'IT-ROM'},
                    {'name': 'Milan', 'lat': 45.4642, 'lon': 9.1900, 'subzone_code': 'IT-MIL'},
                    {'name': 'Naples', 'lat': 40.8518, 'lon': 14.2681, 'subzone_code': 'IT-NAP'},
                    {'name': 'Turin', 'lat': 45.0703, 'lon': 7.6869, 'subzone_code': 'IT-TUR'}
                ]
            },
            'LV': {  # Latvia
                'name': 'Latvia',
                'subzones': [
                    {'name': 'Riga', 'lat': 56.9496, 'lon': 24.1052, 'subzone_code': 'LV-RIG'},
                    {'name': 'Daugavpils', 'lat': 55.8747, 'lon': 26.5059, 'subzone_code': 'LV-DAU'},
                    {'name': 'Liepaja', 'lat': 56.5053, 'lon': 21.0107, 'subzone_code': 'LV-LIE'}
                ]
            },
            'LT': {  # Lithuania
                'name': 'Lithuania',
                'subzones': [
                    {'name': 'Vilnius', 'lat': 54.6872, 'lon': 25.2797, 'subzone_code': 'LT-VIL'},
                    {'name': 'Kaunas', 'lat': 54.8985, 'lon': 23.9036, 'subzone_code': 'LT-KAU'},
                    {'name': 'Klaipeda', 'lat': 55.7172, 'lon': 21.1175, 'subzone_code': 'LT-KLA'}
                ]
            },
            'LU': {  # Luxembourg
                'name': 'Luxembourg',
                'subzones': [
                    {'name': 'Luxembourg City', 'lat': 49.6116, 'lon': 6.1319, 'subzone_code': 'LU-LUX'},
                    {'name': 'Esch-sur-Alzette', 'lat': 49.4958, 'lon': 5.9809, 'subzone_code': 'LU-ESC'},
                    {'name': 'Dudelange', 'lat': 49.4814, 'lon': 6.0837, 'subzone_code': 'LU-DUD'}
                ]
            },
            'MT': {  # Malta
                'name': 'Malta',
                'subzones': [
                    {'name': 'Valletta', 'lat': 35.8989, 'lon': 14.5146, 'subzone_code': 'MT-VAL'},
                    {'name': 'Sliema', 'lat': 35.9122, 'lon': 14.5019, 'subzone_code': 'MT-SLI'},
                    {'name': 'Birkirkara', 'lat': 35.8972, 'lon': 14.4611, 'subzone_code': 'MT-BIR'}
                ]
            },
            'NL': {  # Netherlands
                'name': 'Netherlands',
                'subzones': [
                    {'name': 'Amsterdam', 'lat': 52.3676, 'lon': 4.9041, 'subzone_code': 'NL-AMS'},
                    {'name': 'Rotterdam', 'lat': 51.9244, 'lon': 4.4777, 'subzone_code': 'NL-ROT'},
                    {'name': 'The Hague', 'lat': 52.0705, 'lon': 4.3007, 'subzone_code': 'NL-HAG'},
                    {'name': 'Utrecht', 'lat': 52.0907, 'lon': 5.1214, 'subzone_code': 'NL-UTR'}
                ]
            },
            'PL': {  # Poland
                'name': 'Poland',
                'subzones': [
                    {'name': 'Warsaw', 'lat': 52.2297, 'lon': 21.0122, 'subzone_code': 'PL-WAW'},
                    {'name': 'Krakow', 'lat': 50.0647, 'lon': 19.9450, 'subzone_code': 'PL-KRK'},
                    {'name': 'Gdansk', 'lat': 54.3520, 'lon': 18.6466, 'subzone_code': 'PL-GDN'},
                    {'name': 'Wroclaw', 'lat': 51.1079, 'lon': 17.0385, 'subzone_code': 'PL-WRO'}
                ]
            },
            'PT': {  # Portugal
                'name': 'Portugal',
                'subzones': [
                    {'name': 'Lisbon', 'lat': 38.7223, 'lon': -9.1393, 'subzone_code': 'PT-LIS'},
                    {'name': 'Porto', 'lat': 41.1579, 'lon': -8.6291, 'subzone_code': 'PT-POR'},
                    {'name': 'Braga', 'lat': 41.5454, 'lon': -8.4265, 'subzone_code': 'PT-BRA'}
                ]
            },
            'RO': {  # Romania
                'name': 'Romania',
                'subzones': [
                    {'name': 'Bucharest', 'lat': 44.4268, 'lon': 26.1025, 'subzone_code': 'RO-BUC'},
                    {'name': 'Cluj-Napoca', 'lat': 46.7712, 'lon': 23.6236, 'subzone_code': 'RO-CLU'},
                    {'name': 'Timisoara', 'lat': 45.7489, 'lon': 21.2087, 'subzone_code': 'RO-TIM'},
                    {'name': 'Constanta', 'lat': 44.1598, 'lon': 28.6348, 'subzone_code': 'RO-CON'}
                ]
            },
            'SK': {  # Slovakia
                'name': 'Slovakia',
                'subzones': [
                    {'name': 'Bratislava', 'lat': 48.1486, 'lon': 17.1077, 'subzone_code': 'SK-BRA'},
                    {'name': 'Kosice', 'lat': 48.7164, 'lon': 21.2611, 'subzone_code': 'SK-KOS'},
                    {'name': 'Zilina', 'lat': 49.2231, 'lon': 18.7393, 'subzone_code': 'SK-ZIL'}
                ]
            },
            'SI': {  # Slovenia
                'name': 'Slovenia',
                'subzones': [
                    {'name': 'Ljubljana', 'lat': 46.0569, 'lon': 14.5058, 'subzone_code': 'SI-LJU'},
                    {'name': 'Maribor', 'lat': 46.5547, 'lon': 15.6459, 'subzone_code': 'SI-MAR'},
                    {'name': 'Celje', 'lat': 46.2311, 'lon': 15.2683, 'subzone_code': 'SI-CEL'}
                ]
            },
            'ES': {  # Spain
                'name': 'Spain',
                'subzones': [
                    {'name': 'Madrid', 'lat': 40.4168, 'lon': -3.7038, 'subzone_code': 'ES-MAD'},
                    {'name': 'Barcelona', 'lat': 41.3851, 'lon': 2.1734, 'subzone_code': 'ES-BCN'},
                    {'name': 'Seville', 'lat': 37.3891, 'lon': -5.9845, 'subzone_code': 'ES-SEV'},
                    {'name': 'Valencia', 'lat': 39.4699, 'lon': -0.3763, 'subzone_code': 'ES-VAL'}
                ]
            },
            'SE': {  # Sweden
                'name': 'Sweden',
                'subzones': [
                    {'name': 'Stockholm', 'lat': 59.3293, 'lon': 18.0686, 'subzone_code': 'SE-STO'},
                    {'name': 'Gothenburg', 'lat': 57.7089, 'lon': 11.9746, 'subzone_code': 'SE-GOT'},
                    {'name': 'Malmo', 'lat': 55.6050, 'lon': 13.0038, 'subzone_code': 'SE-MAL'},
                    {'name': 'Uppsala', 'lat': 59.8586, 'lon': 17.6389, 'subzone_code': 'SE-UPP'}
                ]
            }
        }
        
        self.rate_limiter = WeightedRateLimiter(LIMITS)

        # Open-Meteo hourly variable -> staging column
        self.hourly_params = {
            'temperature_2m': 'temperature_avg',
            'relative_humidity_2m': 'humidity',
            'precipitation': 'precipitation',
            'wind_speed_10m': 'wind_speed',
            'wind_direction_10m': 'wind_direction',
            'pressure_msl': 'air_pressure',
            'cloud_cover': 'cloud_cover',
            'shortwave_radiation': 'solar_radiation',  # W/m², mean of the preceding hour
            'weather_code': 'weather_code',            # WMO weather interpretation code
        }
    
    def request_weight(self, days: int) -> float:
        """Weighted API calls of one request (Open-Meteo counting, see LIMITS)."""
        return max(1.0, days / 14) * max(1.0, len(self.hourly_params) / 10)

    def _make_request(self, params: Dict, weight: float) -> Dict:
        """GET with weighted rate limit and retries; raises RuntimeError when it still fails."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            self.rate_limiter.wait(weight, self.logger)
            try:
                response = requests.get(self.base_url, params=params, timeout=REQUEST_TIMEOUT)
                if response.status_code == 200:
                    return response.json()
                reason = response.text[:300]
                last_error = f"HTTP {response.status_code}: {reason}"
                if 'Daily API request limit' in reason:
                    break                     # nothing to gain from waiting within this run
                if response.status_code == 400:
                    break                     # invalid request
                wait = (300 if 'Hourly' in reason else 5 * (attempt + 1) if 'concurrent' in reason
                        else 60 if response.status_code == 429 else 5 * 2 ** attempt)
            except requests.exceptions.RequestException as e:
                last_error = str(e)
                wait = 5 * 2 ** attempt
            if attempt < MAX_RETRIES - 1:
                self.logger.warning(f"Request failed (attempt {attempt + 1}/{MAX_RETRIES}), retrying in {wait} s: {last_error}")
                time.sleep(wait)
        raise RuntimeError(last_error)

    def fetch_location(self, country_code: str, zone_name: str, subzone: Dict,
                       start: datetime, end: datetime) -> pd.DataFrame:
        """
        Hourly weather for one city and [start, end) from the archive API (real historical weather).
        All timestamps are UTC (timezone=GMT), matching the ENTSO-E data. Raises when the request fails.
        """
        last_day = (end - timedelta(microseconds=1)).date()      # the API's end_date is inclusive
        params = {
            'latitude': subzone['lat'],
            'longitude': subzone['lon'],
            'start_date': start.strftime('%Y-%m-%d'),
            'end_date': last_day.strftime('%Y-%m-%d'),
            'hourly': ','.join(self.hourly_params),
            'timezone': 'GMT',
        }
        days = (last_day - start.date()).days + 1
        response_data = self._make_request(params, self.request_weight(days))
        if 'hourly' not in response_data:
            raise RuntimeError(f"no hourly data in response: {str(response_data)[:200]}")

        hourly = response_data['hourly']
        weather_df = pd.DataFrame({column: hourly.get(api_param) for api_param, column in self.hourly_params.items()})
        weather_df.insert(0, 'timestamp', pd.to_datetime(hourly['time']))
        weather_df = weather_df[(weather_df['timestamp'] >= start) & (weather_df['timestamp'] < end)]
        weather_df = weather_df.assign(
            latitude=subzone['lat'],
            longitude=subzone['lon'],
            subzone_code=subzone['subzone_code'],
            subzone_name=subzone['name'],
            country_code=country_code,
            zone_name=zone_name,
        )
        # Weather condition from the official WMO code (unknown codes -> 'Unknown')
        weather_df['weather_condition'] = weather_df['weather_code'].map(WMO_CONDITIONS).fillna('Unknown')
        return weather_df

    # Columns of staging_weather_data written by this script (table defined in database/create_warehouse.sql)
    STAGING_COLUMNS = ['timestamp', 'country_code', 'zone_name', 'subzone_code', 'subzone_name',
                       'temperature_avg', 'humidity', 'precipitation', 'wind_speed', 'wind_direction',
                       'air_pressure', 'cloud_cover', 'solar_radiation', 'weather_code', 'weather_condition',
                       'latitude', 'longitude']

    @staticmethod
    def year_chunks(start: datetime, end: datetime) -> List[Tuple[datetime, datetime]]:
        """Split [start, end) at calendar-year boundaries."""
        chunks, chunk_start = [], start
        while chunk_start < end:
            chunk_end = min(datetime(chunk_start.year + 1, 1, 1), end)
            chunks.append((chunk_start, chunk_end))
            chunk_start = chunk_end
        return chunks

    def extract_to_staging(self, start: datetime, end: datetime) -> Dict:
        """
        Fetch hourly weather for all cities and [start, end) per city and calendar year (parallel, paced to
        the API limits) and write each chunk to staging_weather_data as it arrives (table emptied first).

        Returns {'rows': n, 'failed': [...]}
        """
        cities = [(country_code, zone_info['name'], subzone)
                  for country_code, zone_info in self.weather_locations.items()
                  for subzone in zone_info['subzones']]
        chunks = self.year_chunks(start, end)
        tasks = [(country_code, zone_name, subzone, chunk_start, chunk_end)
                 for country_code, zone_name, subzone in cities
                 for chunk_start, chunk_end in chunks]
        total_weight = sum(self.request_weight((chunk_end - chunk_start).days or 1) for *_, chunk_start, chunk_end in tasks)
        self.logger.info(f"Open-Meteo {start:%Y-%m-%d} -> {end:%Y-%m-%d} (end exclusive): {len(cities)} cities x "
                         f"{len(chunks)} chunks = {len(tasks)} requests, ~{total_weight:,.0f} weighted API calls "
                         f"(limits: {LIMITS[3600]:,}/hour, {DAILY_LIMIT:,}/day)")
        if total_weight > DAILY_LIMIT:
            self.logger.warning("This period needs more than the daily API limit - split it over several days")

        result = {'rows': 0, 'failed': []}
        conn = pyodbc.connect(self.connection_string)
        try:
            cursor = conn.cursor()
            if cursor.execute("SELECT OBJECT_ID('dbo.staging_weather_data', 'U')").fetchone()[0] is None:
                raise RuntimeError("staging_weather_data does not exist - run database/create_warehouse.sql first")
            cursor.execute("TRUNCATE TABLE dbo.staging_weather_data")
            conn.commit()
            cursor.fast_executemany = True
            insert_sql = (f"INSERT INTO dbo.staging_weather_data ({', '.join(self.STAGING_COLUMNS)}) "
                          f"VALUES ({', '.join('?' for _ in self.STAGING_COLUMNS)})")

            started = time.time()
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(self.fetch_location, *task): task for task in tasks}
                for done, future in enumerate(as_completed(futures), 1):
                    _, _, subzone, chunk_start, _ = futures[future]
                    label = f"{subzone['name']} {chunk_start:%Y-%m-%d}"
                    try:
                        weather_df = future.result()
                    except Exception as e:
                        self.logger.error(f"FAILED {label}: {e}")
                        result['failed'].append(f"{label}: {e}")
                        continue
                    df = weather_df[self.STAGING_COLUMNS].astype(object)
                    df = df.where(pd.notna(df), None)  # NaN -> NULL
                    rows = [(ts.to_pydatetime(), *rest) for ts, *rest in df.itertuples(index=False, name=None)]
                    if rows:
                        cursor.executemany(insert_sql, rows)
                        conn.commit()
                    result['rows'] += len(rows)
                    if done % 25 == 0 or done == len(tasks):
                        self.logger.info(f"{done}/{len(tasks)} requests done ({(time.time() - started) / 60:.1f} min), "
                                         f"{result['rows']:,} rows")
        finally:
            conn.close()
        return result


def main():
    """Entry point for SSIS and manual runs: python open_meteo_client.py <start_date> <end_date>."""
    start_run_log('open_meteo_client')
    import sys
    import os
    from datetime import datetime, timedelta
    
    # connection string from the environment (SSIS passes none)
    connection_string = os.getenv('DW_CONNECTION_STRING', '') or os.getenv('ConnectionString', '')
   
    print("Connection string successfully read")
   
    # fall back to the local default when missing or not an ODBC string
    if not connection_string or ('Driver' not in connection_string and 'DSN' not in connection_string):
        connection_string = "Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=EnergyWeatherDW;Trusted_Connection=yes;TrustServerCertificate=yes;"
        print(f"Using default connection string: {connection_string}")
    
    # default period: the last 36 hours
    end_date = datetime.now().replace(minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(hours=36)
    
    # dates from the arguments (optional)
    if len(sys.argv) > 1:
        try:
            start_date_str = sys.argv[1]
            if start_date_str.startswith('"') and start_date_str.endswith('"'):
                start_date_str = start_date_str[1:-1]
            start_date = datetime.fromisoformat(start_date_str)
            print(f"Using start_date from arguments: {start_date}")
        except Exception as e:
            print(f"Error parsing start_date: {e}, using default: {start_date}")
    
    if len(sys.argv) > 2:
        try:
            end_date_str = sys.argv[2]
            if end_date_str.startswith('"') and end_date_str.endswith('"'):
                end_date_str = end_date_str[1:-1]
            end_date = datetime.fromisoformat(end_date_str)
            print(f"Using end_date from arguments: {end_date}")
        except Exception as e:
            print(f"Error parsing end_date: {e}, using default: {end_date}")
    
    print(f"Data extraction period: {start_date} to {end_date}")
    print("Starting EU weather data extraction for all 27 EU countries...")
    
    with EtlStep('WEATHER_EXTRACT', connection_string) as step:
        try:
            client = OpenMeteoClient(connection_string)
            result = client.extract_to_staging(start_date, end_date)
            step.records = result['rows']
            if result['failed']:
                step.details = f"FAILED {len(result['failed'])} requests: " + '; '.join(result['failed'][:5])
                print(f"{len(result['failed'])} requests failed - staging is incomplete")
                sys.exit(1)
            print(f"EU weather data extraction completed successfully: {result['rows']:,} rows")
            sys.exit(0)

        except Exception as e:
            print(f"Fatal error: {str(e)}")
            import traceback
            print(traceback.format_exc())
            sys.exit(1)

if __name__ == "__main__":
    main()