# ENTSO-E Weather Data Platform

A data warehouse that combines European electricity data (ENTSO-E), weather data (Open-Meteo) and
socio-economic indicators (Eurostat) for 8 countries (PL, DE, CZ, SK, HU, FR, ES, IT), loaded into SQL Server
and reported in Power BI.

## Architecture

```
ENTSO-E API ─┐
Open-Meteo ──┼─► Python extract scripts ─► staging_* tables ─► dimension / fact processing ─► dim_* + fact_energy_weather ─► Power BI
Eurostat ────┘                      (orchestrated by SSIS package Main_ETL_Process.dtsx)
```

## Repository layout

| Path | Contents |
|---|---|
| `database/create_warehouse.sql` | Creates the `EnergyWeatherDW` database: star schema, indexes, `etl_log`, stored procedures |
| `etl/` | Python scripts run by SSIS, in pipeline order below |
| `ssis/` | SSIS project (`EnergyWeatherETL.sln`), package `Main_ETL_Process.dtsx` |
| `powerbi/` | Power BI report (`.pbix`, not committed — see below) |
| `docs/` | Project documentation |

### Pipeline order (`Main_ETL_Process.dtsx`)

1. `DW_connection` — checks `etl_log` that no other run is in progress
2. `etl/entsoe_client.py` — load, forecast and generation from ENTSO-E → `staging_entso_*`
3. `etl/open_meteo_client.py` — weather & climate → `staging_weather_data`, `staging_climate_data`
4. `etl/eurostat_client.py` — population, GDP, prices, etc. → `staging_eurostat_*`
5. `etl/dimension_processor.py` — builds staging dimensions
6. `etl/fact_processor.py` — builds `staging_fact_energy_weather`
7. `etl/warehouse_builder.py` — rebuilds final `dim_*` / `fact_energy_weather` from staging
8. `etl/fix_null_values.py` — replaces NULLs with defaults

## Setup

1. Install SQL Server (Developer edition), SSMS, and Visual Studio 2022 with the
   *SQL Server Integration Services Projects* extension.
2. Run `database/create_warehouse.sql` in SSMS.
3. `pip install -r requirements.txt`
4. Set environment variables (see `.env.example`): `ENTSO_TOKEN` (required), `DW_CONNECTION_STRING` (optional).
5. Open `ssis/EnergyWeatherETL.sln`. Check the package variables `PythonPath` (default `python.exe` on PATH),
   `ScriptsFolder` (path to `etl/`), `StartDate`, `EndDate`, `Countries`, then run `Main_ETL_Process.dtsx`.

Each script can also be run on its own from `etl/`, e.g. `python open_meteo_client.py 2023-12-30 2024-01-02`.

## Power BI

The report (`powerbi/EnergyWeatherReport.pbix`, ~400 MB in import mode) is excluded from git because it
exceeds GitHub's 100 MB file limit. It has four pages — Generation, Renewable Energy Sources,
Consumption, Weather — built on `fact_energy_weather` and the `dim_*` tables.
