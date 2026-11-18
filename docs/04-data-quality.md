# 04 Logging and data quality

[← Data model](03-data-model.md) · **Logging and data quality** · [Power BI report →](05-power-bi-report.md)

Every run leaves a trace that can be checked with one query, and every gap in the data is explained by the
source, not by the pipeline.

## The run log: `etl_log`

One table records every package run and every step:

- **Start Run** opens an `ETL_RUN` row. It refuses to start while another run is in progress, and closes
  rows left RUNNING by a crashed run as ABANDONED.
- **Every step** writes its own row, RUNNING → SUCCESS or FAILED, linked to the run by `run_id`, with the
  number of rows and details: rows per data type, days ENTSO-E could not serve, rows inserted and updated,
  unmatched keys.
- **Finish Run** closes the run as SUCCESS; the OnError handler closes it, and any unfinished step, as
  FAILED.

```sql
SELECT log_id, run_id, process_name, status, records_processed, start_time, end_time,
       execution_details, error_message
FROM dbo.etl_log
ORDER BY log_id DESC;
```

Python scripts log through one context manager (`with EtlStep('NAME', connection) as step:` in
[`etl/etl_log.py`](../etl/etl_log.py)), the T-SQL procedures through `usp_etl_step_start` /
`usp_etl_step_end`. Scripts run by hand outside SSIS are logged too, with an empty `run_id`.

## Run log files

SSIS does not keep the console output of the scripts it runs, so a failed step would otherwise leave no
trace. Every script copies its full output, including tracebacks, to
`etl/log/<script>_<date>_<time>.log` (the last 30 per script are kept; tokens and passwords are masked),
and a FAILED `etl_log` row names its log file.

[`tools/check_generated_files.py`](../tools/check_generated_files.py) lists every file outside the
repository (logs, exports, leftovers) with where it comes from, and flags anything unexpected:
`python tools/check_generated_files.py --since 2h` after a run.

## Checks built into the pipeline

| Check | Where | Effect |
|---|---|---|
| ENTSO-E request fails (other than "no data") | extract | the step fails; no partial load passes as complete |
| Eurostat query fails or returns nothing | extract | the step fails |
| staging table missing or empty | extract / load | the step fails or reports it in its details |
| a key that cannot be matched | load facts | points to "Unknown" (key 0) and is counted per dimension |
| a fact key is NULL | Key Check | set to "Unknown" and counted; key columns are `NOT NULL` |
| duplicate fact | schema | rejected by the primary key on the grain |
| key without a dimension row | schema | rejected by the foreign key |

## Tests

The load rules were tested on a separate test database before the real load:

- **Idempotency:** the same period loaded twice → 0 new rows, 0 updates.
- **Corrections:** a changed load value and a changed generation value → exactly one update each.
- **No erasing:** a reload with a missing forecast or missing weather keeps the stored values.
- **SCD Type 2:** a revised population figure → one new version, the old one closed, facts untouched.
- **Partial Eurostat runs:** one country only, a later start year, or an empty staging table → 0 changes
  to the other country-years.
- **Totals:** load and generation totals in the warehouse = totals in staging.
- **Crashes:** a failing step, a killed step and a crashed run are all closed correctly; a second run is
  refused while one is running.

## Coverage

Coverage is the share of the 26,304 hours of 2023–2025 that have a value. **22 of the 28 bidding zones have
at least 99.7 %** for load, forecast, generation and weather. The gaps are at the source:

| Zone | Gap |
|---|---|
| Bulgaria, Malta | ENTSO-E publishes no data; excluded from the report |
| Cyprus | almost no data before mid-2025 |
| Ireland | day-ahead forecasts for only about 3 % of hours; load missing in some months (2023-04/05, 2025-01/02/03/11/12) |
| Slovenia | forecasts for about 98 % of hours |
| Spain | 35 hours missing during the blackout of 28 April 2025 |
| Netherlands, Ireland | almost no solar generation reported (rooftop solar is not metered at the grid level) |

How the report handles the gaps: it never shows a missing value as 0, every page footer names its gaps,
and per-person figures only use country-years with at least 95 % of hours covered (see
[Power BI report](05-power-bi-report.md#how-the-figures-are-calculated)).

---

[← Data model](03-data-model.md) · [Power BI report →](05-power-bi-report.md)
