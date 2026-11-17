"""
Logging shared by all ETL scripts:

1. Run log file (etl/log/):

       start_run_log('entsoe_client')   # first thing in main()

   Everything the script writes to the console (logging messages, print() output, tracebacks) is
   also written to etl/log/<script>_<yyyymmdd_hhmmss>.log. SSIS does not keep the console output of
   the scripts, so this file is where to look when a step fails. The last KEEP_LOG_FILES files per
   script are kept, older ones deleted.

2. One row per ETL step in dbo.etl_log (step 2.3):

       with EtlStep('WEATHER_EXTRACT', connection_string) as step:
           ...                      # the work of the script
           step.records = 8544      # optional: rows written
           step.details = '...'     # optional: short summary

   On entry a RUNNING row is written (linked to the SSIS package run in progress, if any).
   On exit the row becomes SUCCESS, or FAILED when the block raises an exception or calls
   sys.exit() with a non-zero code; the exception itself is not swallowed. A FAILED row names
   the run log file. The procedures are defined in database/create_warehouse.sql.
"""

import atexit
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import pyodbc

logger = logging.getLogger("etl_log")

LOG_DIR = Path(__file__).resolve().parent / "log"
KEEP_LOG_FILES = 30
run_log_path = None          # set by start_run_log()


class _Tee:
    """Write to the console stream and to the run log file."""

    def __init__(self, console, file):
        self.console, self.file = console, file

    def write(self, text):
        if self.console is not None:
            try:
                self.console.write(text)
            except (OSError, ValueError, UnicodeEncodeError):
                pass        # console missing or unable to show a character: the file still gets it
        self.file.write(text)
        return len(text)

    def flush(self):
        for stream in (self.console, self.file):
            try:
                if stream is not None:
                    stream.flush()
            except (OSError, ValueError):
                pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return "utf-8"


def _masked(argv: list) -> list:
    """Command line without secrets: the value of --token and PWD=/Password= in connection strings."""
    out, hide_next = [], False
    for arg in argv:
        if hide_next:
            out.append("***")
            hide_next = False
        elif arg.startswith("--token"):
            out.append("--token=***" if "=" in arg else arg)
            hide_next = "=" not in arg
        else:
            out.append(re.sub(r"(?i)\b(pwd|password)=[^;]*", r"\1=***", arg))
    return out


def start_run_log(script_name: str) -> Path:
    """Copy all console output of this process to etl/log/<script>_<timestamp>.log."""
    global run_log_path
    LOG_DIR.mkdir(exist_ok=True)
    run_log_path = LOG_DIR / f"{script_name}_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_file = open(run_log_path, "a", encoding="utf-8", buffering=1)
    atexit.register(log_file.close)

    console_out, console_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(console_out, log_file), _Tee(console_err, log_file)
    # logging handlers created before this call still point at the old console streams
    for handler in logging.getLogger().handlers:
        if type(handler) is logging.StreamHandler:
            handler.setStream(sys.stderr if handler.stream is console_err else sys.stdout)

    print(f"Run log: {run_log_path}  (command: {' '.join(_masked(sys.argv))})")
    for old in sorted(LOG_DIR.glob(f"{script_name}_*.log"))[:-KEEP_LOG_FILES]:
        try:
            old.unlink()
        except OSError:
            pass
    return run_log_path


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
        see = f"see {run_log_path}" if run_log_path else "see the script output"
        if exc_type is None or (exc_type is SystemExit and exc.code in (0, None)):
            status, error = 'SUCCESS', None
        elif exc_type is SystemExit:
            status, error = 'FAILED', f"Exit code {exc.code} ({see})"
        else:
            status, error = 'FAILED', f"{exc_type.__name__}: {exc} ({see})"
        try:
            self._execute("EXEC dbo.usp_etl_step_end ?, ?, ?, ?, ?",
                          self.log_id, status, int(self.records or 0), self.details, error)
            logger.info(f"etl_log: {self.process_name} {status}")
        except Exception as e:
            logger.error(f"etl_log: could not log the end of {self.process_name}: {e}")
            if exc_type is None:
                raise
        return False
