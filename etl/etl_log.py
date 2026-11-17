"""
Logging of one ETL step to dbo.etl_log (step 2.3), shared by all ETL scripts.

    with EtlStep('WEATHER_EXTRACT', connection_string) as step:
        ...                      # the work of the script
        step.records = 8544      # optional: rows written
        step.details = '...'     # optional: short summary

On entry a RUNNING row is written (linked to the SSIS package run in progress, if any).
On exit the row becomes SUCCESS, or FAILED when the block raises an exception or calls
sys.exit() with a non-zero code; the exception itself is not swallowed.
The procedures are defined in database/create_warehouse.sql.
"""

import logging

import pyodbc

logger = logging.getLogger("etl_log")


class EtlStep:
    def __init__(self, process_name: str, connection_string: str):
        self.process_name = process_name
        self.connection_string = connection_string
        self.records = 0
        self.details = None
        self.log_id = None

    def _execute(self, sql: str, *params):
        conn = pyodbc.connect(self.connection_string, autocommit=True)
        try:
            cursor = conn.cursor().execute(sql, *params)
            return cursor.fetchone() if cursor.description else None
        finally:
            conn.close()

    def __enter__(self):
        self.log_id = self._execute("SET NOCOUNT ON; DECLARE @id BIGINT; "
                                    "EXEC dbo.usp_etl_step_start ?, @id OUTPUT; SELECT @id;", self.process_name)[0]
        logger.info(f"etl_log: {self.process_name} started (log_id {self.log_id})")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None or (exc_type is SystemExit and exc.code in (0, None)):
            status, error = 'SUCCESS', None
        elif exc_type is SystemExit:
            status, error = 'FAILED', f"Exit code {exc.code} (see the script output)"
        else:
            status, error = 'FAILED', f"{exc_type.__name__}: {exc}"
        try:
            self._execute("EXEC dbo.usp_etl_step_end ?, ?, ?, ?, ?",
                          self.log_id, status, int(self.records or 0), self.details, error)
            logger.info(f"etl_log: {self.process_name} {status}")
        except Exception as e:
            logger.error(f"etl_log: could not log the end of {self.process_name}: {e}")
            if exc_type is None:
                raise
        return False
