"""
Key check for the fact tables (decision G2: keys can never be NULL).

For every dimension key column of fact_load and fact_generation:
  - NULL keys are set to 0 (the "Unknown" row) and counted
  - the column is made NOT NULL if it isn't already
  - rows pointing to the "Unknown" row (key 0) are counted

Measures are never touched: unknown values stay NULL (decision G).
The result is written to etl_log (process KEY_CHECK). Exit code 1 if the check itself fails.

Usage (SSIS task "Key Check"):
    python fix_null_values.py
"""

import logging
import os
import sys

import pyodbc

from etl_log import EtlStep

DEFAULT_CONNECTION_STRING = ("Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=EnergyWeatherDW;"
                             "Trusted_Connection=yes;TrustServerCertificate=yes;")

# fact table -> key columns (date_key and time_key are calculated from the timestamp and always match)
FACT_KEYS = {
    'fact_load': ['date_key', 'time_key', 'bidding_zone_key', 'socioeconomic_profile_key', 'weather_condition_key'],
    'fact_generation': ['date_key', 'time_key', 'bidding_zone_key', 'socioeconomic_profile_key', 'generation_type_key'],
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("key_check")


def check_table(cursor, table: str, key_columns: list) -> list:
    findings = []
    columns = {row.column_name: row for row in cursor.columns(table=table, schema='dbo')}
    for column in key_columns:
        info = columns[column]

        nulls = cursor.execute(f"SELECT COUNT(*) FROM dbo.{table} WHERE {column} IS NULL").fetchone()[0]
        if nulls:
            cursor.execute(f"UPDATE dbo.{table} SET {column} = 0 WHERE {column} IS NULL")
            findings.append(f"{table}.{column}: {nulls} NULL keys set to 0")
            logger.warning(findings[-1])

        if info.nullable:
            cursor.execute(f"ALTER TABLE dbo.{table} ALTER COLUMN {column} {info.type_name} NOT NULL")
            findings.append(f"{table}.{column}: made NOT NULL")
            logger.warning(findings[-1])

        if column not in ('date_key', 'time_key'):
            unknown = cursor.execute(f"SELECT COUNT(*) FROM dbo.{table} WHERE {column} = 0").fetchone()[0]
            if unknown:
                findings.append(f"{table}.{column}: {unknown} rows -> Unknown")
                logger.info(findings[-1])
    return findings


def main():
    connection_string = os.getenv('DW_CONNECTION_STRING') or DEFAULT_CONNECTION_STRING
    try:
        with EtlStep('KEY_CHECK', connection_string) as step:
            with pyodbc.connect(connection_string) as conn:
                cursor = conn.cursor()
                findings = []
                for table, key_columns in FACT_KEYS.items():
                    findings += check_table(cursor, table, key_columns)
                conn.commit()
            step.details = '; '.join(findings) if findings else 'all keys matched'
            logger.info(f"Key check finished: {step.details}")
    except Exception as e:
        logger.error(f"Key check failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
