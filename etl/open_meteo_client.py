"""
OpenMeteoClient.py
Klient API do komunikacji z Open-Meteo
Pobiera historyczne dane pogodowe i wskaźniki klimatyczne
Rozszerzony o wszystkie kraje Unii Europejskiej
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
from concurrent.futures import ThreadPoolExecutor, as_completed

from etl_log import EtlStep

# Parallel requests to the archive API (free tier: 600 calls/minute)
MAX_WORKERS = 8

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
    """Klient API dla Open-Meteo Weather Service"""
    
    def __init__(self, connection_string: str):
        """
        Inicjalizacja klienta Open-Meteo
        
        Args:
            connection_string: String połączenia z bazą danych
        """
        # Archive API = real historical weather. (The climate API is model simulation; not used.)
        self.base_url = "https://archive-api.open-meteo.com/v1/archive"
        self.connection_string = connection_string
        
        # Konfiguracja logowania
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
        
        # Rozszerzona mapa lokalizacji - wszystkie kraje UE z reprezentatywnymi miastami
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
    
    def _make_request(self, url: str, params: Dict, max_retries: int = 3) -> Optional[Dict]:
        """
        Wykonanie zapytania do API z retry logic
        
        Args:
            url: URL endpoint
            params: Parametry zapytania
            max_retries: Maksymalna liczba prób
            
        Returns:
            Odpowiedź JSON jako dict lub None w przypadku błędu
        """
        for attempt in range(max_retries):
            try:
                response = requests.get(url, params=params, timeout=30)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:  # Too Many Requests
                    wait_time = 2 ** attempt
                    self.logger.warning(f"Rate limit hit, waiting {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    self.logger.error(f"HTTP Error {response.status_code}: {response.text}")
                    
            except requests.exceptions.RequestException as e:
                self.logger.error(f"Request failed (attempt {attempt + 1}): {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    
        return None
    
    def fetch_location(self, country_code: str, zone_name: str, subzone: Dict,
                       start_date: datetime, end_date: datetime) -> pd.DataFrame:
        """
        Fetch hourly weather for one city from the archive API (real historical weather).
        All timestamps are UTC (timezone=GMT), matching the ENTSO-E data.

        Returns:
            hourly weather DataFrame; empty DataFrame on failure
        """
        params = {
            'latitude': subzone['lat'],
            'longitude': subzone['lon'],
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d'),
            'hourly': ','.join(self.hourly_params),
            'timezone': 'GMT',
        }

        response_data = self._make_request(self.base_url, params)
        if not response_data or 'hourly' not in response_data:
            self.logger.warning(f"No weather data received for {subzone['name']}")
            return pd.DataFrame()

        hourly = response_data['hourly']
        weather_df = pd.DataFrame({column: hourly.get(api_param) for api_param, column in self.hourly_params.items()})
        weather_df.insert(0, 'timestamp', pd.to_datetime(hourly['time']))
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

    def extract_all(self, start_date: datetime, end_date: datetime) -> pd.DataFrame:
        """Fetch hourly weather for all cities in parallel."""
        cities = [(country_code, zone_info['name'], subzone)
                  for country_code, zone_info in self.weather_locations.items()
                  for subzone in zone_info['subzones']]
        self.logger.info(f"Fetching weather for {len(self.weather_locations)} countries, {len(cities)} cities "
                         f"from {start_date:%Y-%m-%d} to {end_date:%Y-%m-%d} ({MAX_WORKERS} parallel requests)")

        parts, failed = [], []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(self.fetch_location, country_code, zone_name, subzone, start_date, end_date): subzone['name']
                       for country_code, zone_name, subzone in cities}
            for future in as_completed(futures):
                city = futures[future]
                try:
                    weather_df = future.result()
                except Exception as e:
                    self.logger.error(f"Error fetching weather for {city}: {str(e)}")
                    weather_df = pd.DataFrame()
                if weather_df.empty:
                    failed.append(city)
                else:
                    parts.append(weather_df)

        self.failed_cities = sorted(failed)
        if failed:
            self.logger.warning(f"No data for {len(failed)} cities: {', '.join(self.failed_cities)}")

        weather_data = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        self.logger.info(f"Extracted {len(weather_data)} weather records")
        return weather_data

    # Columns of staging_weather_data written by this script (table defined in database/create_warehouse.sql)
    STAGING_COLUMNS = ['timestamp', 'country_code', 'zone_name', 'subzone_code', 'subzone_name',
                       'temperature_avg', 'humidity', 'precipitation', 'wind_speed', 'wind_direction',
                       'air_pressure', 'cloud_cover', 'solar_radiation', 'weather_code', 'weather_condition',
                       'latitude', 'longitude']

    def save_to_staging(self, weather_data: pd.DataFrame) -> bool:
        """Replace the contents of staging_weather_data with weather_data (bulk insert)."""
        if weather_data.empty:
            self.logger.warning("No weather data to save")
            return False

        # One backup file, overwritten on every run
        try:
            weather_data.to_csv('staging_weather_data.csv', index=False)
        except Exception as e:
            self.logger.warning(f"Could not write CSV backup: {str(e)}")

        try:
            conn = pyodbc.connect(self.connection_string)
            cursor = conn.cursor()
            if cursor.execute("SELECT OBJECT_ID('dbo.staging_weather_data', 'U')").fetchone()[0] is None:
                raise RuntimeError("staging_weather_data does not exist - run database/create_warehouse.sql first")

            df = weather_data[self.STAGING_COLUMNS].astype(object)
            df = df.where(pd.notna(df), None)  # NaN -> NULL
            rows = [tuple(r) for r in df.itertuples(index=False, name=None)]

            cursor.execute("TRUNCATE TABLE dbo.staging_weather_data")
            cursor.fast_executemany = True
            cursor.executemany(
                f"INSERT INTO dbo.staging_weather_data ({', '.join(self.STAGING_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in self.STAGING_COLUMNS)})", rows)
            conn.commit()
            conn.close()
            self.logger.info(f"Saved {len(rows)} weather records to staging_weather_data")
            return True
        except Exception as e:
            self.logger.error(f"Error saving data to staging: {str(e)}")
            return False


# Funkcja główna do uruchomienia z SSIS
def main():
    """Główna funkcja wywoływana przez SSIS"""
    import sys
    import os
    from datetime import datetime, timedelta
    
    # Parametry z SSIS lub zmiennych środowiskowych
    connection_string = os.getenv('DW_CONNECTION_STRING', '') or os.getenv('ConnectionString', '')
   
    print("Connection string successfully read")
   
    # Sprawdzenie czy connection_string ma odpowiedni format
    if not connection_string or ('Driver' not in connection_string and 'DSN' not in connection_string):
        # Domyślny connection string jako bezpieczna opcja
        connection_string = "Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=EnergyWeatherDW;Trusted_Connection=yes;TrustServerCertificate=yes;"
        print(f"Using default connection string: {connection_string}")
    
    # Domyślnie pobieramy dane z ostatnich 36 godzin
    end_date = datetime.now().replace(minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(hours=36)
    
    # Parametry z argumentów linii komend (opcjonalnie)
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
            # Inicjalizacja klienta
            client = OpenMeteoClient(connection_string)
        
            # Wyświetl informacje o krajach i miastach do przetworzenia
            total_countries = len(client.weather_locations)
            total_cities = sum(len(zone_info['subzones']) for zone_info in client.weather_locations.values())
            print(f"Processing {total_countries} EU countries with {total_cities} cities:")
        
            for country_code, zone_info in client.weather_locations.items():
                cities = [subzone['name'] for subzone in zone_info['subzones']]
                print(f"  {zone_info['name']} ({country_code}): {', '.join(cities)}")
        
            print("\nStarting weather data extraction...")
            weather_data = client.extract_all(start_date, end_date)

            if weather_data.empty:
                print("Warning: No weather data extracted")
            else:
                print(f"Successfully extracted {len(weather_data)} weather records for all EU subzones")
            
                # Podsumowanie danych per kraj
                country_summary = weather_data.groupby('country_code').size().reset_index(name='records')
                print("\nWeather data summary by country:")
                for _, row in country_summary.iterrows():
                    country_name = client.weather_locations[row['country_code']]['name']
                    print(f"  {country_name} ({row['country_code']}): {row['records']} records")
        
            # Zapis do staging
            print("\nSaving data to staging tables...")
            success = client.save_to_staging(weather_data)
            step.records = len(weather_data)
            if client.failed_cities:
                step.details = f"No data for {len(client.failed_cities)} cities: {', '.join(client.failed_cities)}"

            if success:
                print("EU weather data extraction completed successfully")
                print(f"Total weather records: {len(weather_data)}")
                sys.exit(0)
            else:
                print("Error occurred during EU weather data extraction")
                sys.exit(1)
            
        except Exception as e:
            print(f"Fatal error: {str(e)}")
            import traceback
            print(traceback.format_exc())
            sys.exit(1)

if __name__ == "__main__":
    main()