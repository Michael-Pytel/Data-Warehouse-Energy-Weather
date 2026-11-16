"""
Single source of truth for the countries and ENTSO-E areas covered by the pipeline.

Every script that needs bidding-zone codes imports them from here, so the codes
used to extract data (entsoe_client.py) and the codes stored in the dimensions
(dimension_processor.py) always match - fact_processor.py joins on them.

EIC codes verified against the entsoe-py `Area` mapping
(https://github.com/EnergieID/entsoe-py/blob/master/entsoe/mappings.py).
"""

# EIC code -> area details. One entry per area; Denmark has two bidding zones.
BIDDING_ZONES = {
    '10YAT-APG------L': {'name': 'Austria',            'country': 'AT', 'timezone': 'Europe/Vienna',     'control_area': 'APG'},
    '10YBE----------2': {'name': 'Belgium',            'country': 'BE', 'timezone': 'Europe/Brussels',   'control_area': 'Elia'},
    '10YBG-ESO------M': {'name': 'Bulgaria',           'country': 'BG', 'timezone': 'Europe/Sofia',      'control_area': 'ESO'},
    '10YHR-HEP------M': {'name': 'Croatia',            'country': 'HR', 'timezone': 'Europe/Zagreb',     'control_area': 'HOPS'},
    '10YCY-1001A0003J': {'name': 'Cyprus',             'country': 'CY', 'timezone': 'Asia/Nicosia',      'control_area': 'Cyprus TSO'},
    '10YCZ-CEPS-----N': {'name': 'Czech Republic',     'country': 'CZ', 'timezone': 'Europe/Prague',     'control_area': 'CEPS'},
    '10YDK-1--------W': {'name': 'Denmark West (DK1)', 'country': 'DK', 'timezone': 'Europe/Copenhagen', 'control_area': 'Energinet'},
    '10YDK-2--------M': {'name': 'Denmark East (DK2)', 'country': 'DK', 'timezone': 'Europe/Copenhagen', 'control_area': 'Energinet'},
    '10Y1001A1001A39I': {'name': 'Estonia',            'country': 'EE', 'timezone': 'Europe/Tallinn',    'control_area': 'Elering'},
    '10YFI-1--------U': {'name': 'Finland',            'country': 'FI', 'timezone': 'Europe/Helsinki',   'control_area': 'Fingrid'},
    '10YFR-RTE------C': {'name': 'France',             'country': 'FR', 'timezone': 'Europe/Paris',      'control_area': 'RTE'},
    '10Y1001A1001A83F': {'name': 'Germany',            'country': 'DE', 'timezone': 'Europe/Berlin',     'control_area': 'Germany (all TSOs)'},
    '10YGR-HTSO-----Y': {'name': 'Greece',             'country': 'GR', 'timezone': 'Europe/Athens',     'control_area': 'IPTO'},
    '10YHU-MAVIR----U': {'name': 'Hungary',            'country': 'HU', 'timezone': 'Europe/Budapest',   'control_area': 'MAVIR'},
    '10Y1001A1001A59C': {'name': 'Ireland (SEM)',      'country': 'IE', 'timezone': 'Europe/Dublin',     'control_area': 'EirGrid / SONI'},
    '10YIT-GRTN-----B': {'name': 'Italy',              'country': 'IT', 'timezone': 'Europe/Rome',       'control_area': 'Terna'},
    '10YLV-1001A00074': {'name': 'Latvia',             'country': 'LV', 'timezone': 'Europe/Riga',       'control_area': 'AST'},
    '10YLT-1001A0008Q': {'name': 'Lithuania',          'country': 'LT', 'timezone': 'Europe/Vilnius',    'control_area': 'Litgrid'},
    '10YLU-CEGEDEL-NQ': {'name': 'Luxembourg',         'country': 'LU', 'timezone': 'Europe/Luxembourg', 'control_area': 'Creos'},
    '10Y1001A1001A93C': {'name': 'Malta',              'country': 'MT', 'timezone': 'Europe/Malta',      'control_area': 'Enemalta'},
    '10YNL----------L': {'name': 'Netherlands',        'country': 'NL', 'timezone': 'Europe/Amsterdam',  'control_area': 'TenneT NL'},
    '10YPL-AREA-----S': {'name': 'Poland',             'country': 'PL', 'timezone': 'Europe/Warsaw',     'control_area': 'PSE'},
    '10YPT-REN------W': {'name': 'Portugal',           'country': 'PT', 'timezone': 'Europe/Lisbon',     'control_area': 'REN'},
    '10YRO-TEL------P': {'name': 'Romania',            'country': 'RO', 'timezone': 'Europe/Bucharest',  'control_area': 'Transelectrica'},
    '10YSK-SEPS-----K': {'name': 'Slovakia',           'country': 'SK', 'timezone': 'Europe/Bratislava', 'control_area': 'SEPS'},
    '10YSI-ELES-----O': {'name': 'Slovenia',           'country': 'SI', 'timezone': 'Europe/Ljubljana',  'control_area': 'ELES'},
    '10YES-REE------0': {'name': 'Spain',              'country': 'ES', 'timezone': 'Europe/Madrid',     'control_area': 'REE'},
    '10YSE-1--------K': {'name': 'Sweden',             'country': 'SE', 'timezone': 'Europe/Stockholm',  'control_area': 'Svenska kraftnat'},
}

# ISO 3166 country codes covered by the pipeline (all 27 EU member states).
EU_COUNTRIES = sorted({zone['country'] for zone in BIDDING_ZONES.values()})

# Eurostat uses its own code for Greece.
EUROSTAT_GEO_CODES = {'GR': 'EL'}
EUROSTAT_TO_ISO = {v: k for k, v in EUROSTAT_GEO_CODES.items()}


def bidding_zones():
    """Return a fresh copy of the zone mapping (safe for callers to modify)."""
    return {code: dict(info) for code, info in BIDDING_ZONES.items()}
