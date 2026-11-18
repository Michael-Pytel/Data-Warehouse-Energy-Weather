# 06 Setup

[← Power BI report](05-power-bi-report.md) · **Setup** · [README](../README.md)

How to build the warehouse and run the pipeline on your own machine. Tested on Windows 11.

## Requirements

| Component | Notes |
|---|---|
| SQL Server 2025 | the free Developer edition is enough; SSMS to run the scripts |
| ODBC Driver 18 for SQL Server | used by the Python scripts |
| OLE DB Driver 19 for SQL Server | used by SSIS (`MSOLEDBSQL19`) |
| Python 3.12 | packages in `requirements.txt` (pandas, numpy, pyodbc, requests, holidays) |
| Visual Studio | with the *SQL Server Integration Services Projects* extension |
| Power BI Desktop | to open the report (tested with version 2.159) |
| ENTSO-E API token | free: register at the [Transparency Platform](https://transparency.entsoe.eu), then *My Account → Web API Security Token* |

The old `Driver={SQL Server}` and the SQL Native Client do not work with SQL Server 2025.

## Installation

1. **Create the database objects.** Run [`database/create_warehouse.sql`](../database/create_warehouse.sql),
   then [`database/load_warehouse.sql`](../database/load_warehouse.sql) (for example in SSMS). Both are safe
   to run again: they create only what is missing and update the procedures.
2. **Install the Python packages:** `pip install -r requirements.txt`
3. **Set the token** as a Windows user environment variable (it never goes into code or the SSIS package):

   ```powershell
   [Environment]::SetEnvironmentVariable("ENTSO_TOKEN", "<your token>", "User")
   ```

   `DW_CONNECTION_STRING` is optional; the default is the local `EnergyWeatherDW` with Windows
   authentication. All settings are listed in [`.env.example`](../.env.example).
4. **Configure the SSIS package.** Open `ssis/EnergyWeatherETL.sln` and check the package variables:

   | Variable | Meaning |
   |---|---|
   | `PythonPath` | Python executable (default `python.exe` on PATH) |
   | `ScriptsFolder` | full path of the `etl/` folder |
   | `StartDate`, `EndDate` | period to load; the end date is exclusive (`2026-01-01` = up to 2025-12-31 23:00 UTC) |
   | `Countries` | comma-separated country codes (default: all 27) |
   | `SinceYear` | first Eurostat year |
   | `ConnectionString` | connection to the warehouse |

## Running the pipeline

Run `Main_ETL_Process.dtsx` in Visual Studio. The full period (2023–2025) takes about **1.5 hours**,
mostly waiting for the APIs; one month takes about 3 minutes. Open-Meteo's free tier allows about 10,000
weighted calls a day and the full period needs about 7,000, so do not run the full period twice on the
same day.

Each step can also run on its own, for example to rerun a failed step or test one source (from `etl/`,
with `ENTSO_TOKEN` set; December 2025 as an example, end date exclusive):

```powershell
python entsoe_client.py --start 2025-12-01 --end 2026-01-01   # 2 Extract ENTSO-E Data
python open_meteo_client.py 2025-12-01 2026-01-01              # 3 Extract Weather Data
python eurostat_client.py 2020                                 # 4 Extract Eurostat Data (all countries since 2020)
python reference_loader.py 2025-12-01 2026-01-01               # 5 Load Reference Data
```

```sql
EXEC dbo.usp_load_dimensions;   -- 6 Load Dimensions
EXEC dbo.usp_load_facts;        -- 7 Load Facts
```

```powershell
python fix_null_values.py                                      # 8 Key Check
```

## Checking a run

- `SELECT * FROM dbo.etl_log ORDER BY log_id DESC;` shows the run and its steps with status, row counts and
  details (see [Logging and data quality](04-data-quality.md)).
- `etl/log/` has the full output of every script run.
- `python tools/check_generated_files.py --since 2h` lists the files a run wrote outside the repository.

## Opening the report

Open `powerbi/Data-Warehouse-Energy-Weather-Report.pbip` in Power BI Desktop and click **Home → Refresh**.
The model connects to `localhost`, database `EnergyWeatherDW`, with Windows authentication. The first
refresh loads 7.9 million fact rows and takes a few minutes. The theme is already registered in the report.

## Troubleshooting

| Problem | Cause and fix |
|---|---|
| a new run is refused ("run in progress") | a run stopped in Visual Studio (■) stays RUNNING and blocks new runs for 12 hours. Close it: `EXEC dbo.usp_etl_run_end <log_id>, 'ABANDONED';` |
| a step failed in SSIS without a message | SSIS keeps no script output: the step's `etl_log` row names its log file in `etl/log/` |
| ENTSO-E answers 401 | `ENTSO_TOKEN` is not set in this terminal; new terminals see a new user variable only after a restart |
| Open-Meteo answers 429 | the hourly or daily limit is reached; the client waits out the hourly limit, the daily limit means waiting until the next day |
| Python cannot find the packages | `python -I` (isolated mode) hides packages installed with `pip install --user` |
| PowerShell shows `NativeCommandError` | PowerShell shows Python's log output on stderr this way; it is not an error |
| Visual Studio overwrote changes to the package | close `Main_ETL_Process.dtsx` without saving after editing it outside Visual Studio |

---

[← Power BI report](05-power-bi-report.md) · [README](../README.md)
