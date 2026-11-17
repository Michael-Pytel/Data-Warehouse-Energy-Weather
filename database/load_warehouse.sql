-- =====================================================================================
-- EnergyWeatherDW: load procedures (staging -> warehouse), decision C (T-SQL transforms)
--
-- Run after create_warehouse.sql. Safe to run repeatedly (CREATE OR ALTER).
--
-- Called by SSIS:
--   EXEC dbo.usp_load_dimensions;   -- task "Load Dimensions"
--   EXEC dbo.usp_load_facts;        -- task "Load Facts"
--
-- Rules (docs/PLAN.md):
--   - facts: new rows are inserted; existing rows are updated only when a reloaded value differs
--     (ENTSO-E revisions, late data), never to NULL or to the Unknown key; updated_at records the change.
--     Re-running a period with unchanged data changes nothing.
--   - SCD Type 2 for dim_bidding_zone and dim_socioeconomic_profile (decision E)
--   - unknown values stay NULL; keys without a match get 0 = "Unknown" and are counted in etl_log (decision G)
--   - weather per country and hour = average of the country's cities (decision A)
-- =====================================================================================

USE EnergyWeatherDW;
GO

-- -------------------------------------------------------------------------------------
-- dim_date: add the missing days between the first and last day found in staging
-- -------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.usp_load_dim_date
    @rows_inserted INT = 0 OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @first DATE, @last DATE;

    SELECT @first = MIN(d), @last = MAX(d)
    FROM (
        SELECT CAST(MIN(timestamp) AS DATE) AS d FROM dbo.staging_entso_actual_load UNION ALL
        SELECT CAST(MAX(timestamp) AS DATE)      FROM dbo.staging_entso_actual_load UNION ALL
        SELECT CAST(MIN(timestamp) AS DATE)      FROM dbo.staging_entso_forecast    UNION ALL
        SELECT CAST(MAX(timestamp) AS DATE)      FROM dbo.staging_entso_forecast    UNION ALL
        SELECT CAST(MIN(timestamp) AS DATE)      FROM dbo.staging_entso_generation  UNION ALL
        SELECT CAST(MAX(timestamp) AS DATE)      FROM dbo.staging_entso_generation  UNION ALL
        SELECT CAST(MIN(timestamp) AS DATE)      FROM dbo.staging_weather_data      UNION ALL
        SELECT CAST(MAX(timestamp) AS DATE)      FROM dbo.staging_weather_data
    ) AS x;

    SET @rows_inserted = 0;
    IF @first IS NULL RETURN;

    WITH n AS (
        SELECT TOP (DATEDIFF(DAY, @first, @last) + 1)
               ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS i
        FROM sys.all_objects a CROSS JOIN sys.all_objects b
    ), d AS (
        SELECT DATEADD(DAY, i, @first) AS full_date,
               DATEDIFF(DAY, '19000101', DATEADD(DAY, i, @first)) % 7 + 1 AS iso_dow   -- 1900-01-01 was a Monday
        FROM n
    )
    INSERT INTO dbo.dim_date (date_key, full_date, year, quarter, month, month_name, year_month,
                              day_of_month, day_of_week, day_name, is_weekend, season)
    SELECT CONVERT(INT, CONVERT(CHAR(8), d.full_date, 112)),
           d.full_date,
           YEAR(d.full_date),
           DATEPART(QUARTER, d.full_date),
           MONTH(d.full_date),
           CHOOSE(MONTH(d.full_date), 'January', 'February', 'March', 'April', 'May', 'June', 'July',
                  'August', 'September', 'October', 'November', 'December'),
           CONVERT(CHAR(7), d.full_date, 126),
           DAY(d.full_date),
           d.iso_dow,
           CHOOSE(d.iso_dow, 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'),
           CASE WHEN d.iso_dow >= 6 THEN 'Yes' ELSE 'No' END,
           CASE WHEN MONTH(d.full_date) IN (12, 1, 2) THEN 'Winter'
                WHEN MONTH(d.full_date) IN (3, 4, 5)  THEN 'Spring'
                WHEN MONTH(d.full_date) IN (6, 7, 8)  THEN 'Summer'
                ELSE 'Autumn' END
    FROM d
    WHERE NOT EXISTS (SELECT 1 FROM dbo.dim_date x WHERE x.full_date = d.full_date);

    SET @rows_inserted = @@ROWCOUNT;
END;
GO

-- -------------------------------------------------------------------------------------
-- dim_bidding_zone (SCD Type 2) from staging_bidding_zone (zones.py)
-- -------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.usp_load_dim_bidding_zone
    @new_versions INT = 0 OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    SELECT s.bidding_zone_code, s.bidding_zone_name, s.country_code, s.country_name, s.timezone,
           HASHBYTES('SHA2_256', CONCAT_WS('|', s.bidding_zone_name, s.country_code, s.country_name, s.timezone)) AS row_hash
    INTO #src
    FROM dbo.staging_bidding_zone s;

    BEGIN TRANSACTION;

    -- 1. close the current version of zones whose attributes changed
    UPDATE h
    SET valid_to = @now, is_current = 'No'
    FROM dbo.dim_bidding_zone_history h
    JOIN #src s ON s.bidding_zone_code = h.bidding_zone_code
    WHERE h.is_current = 'Yes' AND h.row_hash <> s.row_hash;

    -- 2. zones without a current version (new, or just closed) get a new version
    SELECT s.*, k.bidding_zone_key
    INTO #todo
    FROM #src s
    LEFT JOIN (SELECT DISTINCT bidding_zone_code, bidding_zone_key FROM dbo.dim_bidding_zone_history) k
           ON k.bidding_zone_code = s.bidding_zone_code
    WHERE NOT EXISTS (SELECT 1 FROM dbo.dim_bidding_zone_history h
                      WHERE h.bidding_zone_code = s.bidding_zone_code AND h.is_current = 'Yes');

    -- new business keys get the next durable keys
    DECLARE @max_key INT = (SELECT ISNULL(MAX(bidding_zone_key), 0) FROM dbo.dim_bidding_zone);
    WITH t AS (SELECT bidding_zone_key, ROW_NUMBER() OVER (ORDER BY bidding_zone_code) AS rn
               FROM #todo WHERE bidding_zone_key IS NULL)
    UPDATE t SET bidding_zone_key = @max_key + rn;

    INSERT INTO dbo.dim_bidding_zone_history
        (bidding_zone_key, bidding_zone_code, bidding_zone_name, country_code, country_name, timezone,
         row_hash, valid_from, valid_to, is_current)
    SELECT bidding_zone_key, bidding_zone_code, bidding_zone_name, country_code, country_name, timezone,
           row_hash, @now, '9999-12-31', 'Yes'
    FROM #todo;
    SET @new_versions = @@ROWCOUNT;

    -- 3. the dimension holds the current version of every durable key
    MERGE dbo.dim_bidding_zone AS d
    USING (SELECT * FROM dbo.dim_bidding_zone_history WHERE is_current = 'Yes') AS h
       ON d.bidding_zone_key = h.bidding_zone_key
    WHEN MATCHED AND d.valid_from <> h.valid_from THEN
        UPDATE SET bidding_zone_code = h.bidding_zone_code, bidding_zone_name = h.bidding_zone_name,
                   country_code = h.country_code, country_name = h.country_name,
                   timezone = h.timezone, valid_from = h.valid_from
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (bidding_zone_key, bidding_zone_code, bidding_zone_name, country_code, country_name, timezone, valid_from)
        VALUES (h.bidding_zone_key, h.bidding_zone_code, h.bidding_zone_name, h.country_code, h.country_name,
                h.timezone, h.valid_from);

    COMMIT TRANSACTION;
END;
GO

-- -------------------------------------------------------------------------------------
-- dim_socioeconomic_profile (SCD Type 2) from staging_eurostat_raw, one profile per
-- country and year (decision E):
--   - a figure of the year itself is used when Eurostat has it
--   - otherwise the latest earlier year, and the profile is flagged is_estimated = 'Yes'
--   - electricity price = mean of the half-years; only one half-year -> also flagged
--   - no figure at all -> NULL (decision G)
-- Years: every year in the Eurostat staging data and every year of the energy staging data.
-- -------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.usp_load_dim_socioeconomic_profile
    @new_versions INT = 0 OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    -- figures per country, indicator and year (half-years averaged)
    SELECT r.country_code,
           CAST(LEFT(r.time_period, 4) AS SMALLINT) AS year,
           r.dataset_name AS indicator,
           AVG(r.value) AS value,
           COUNT(*) AS n_periods
    INTO #fig
    FROM dbo.staging_eurostat_raw r
    WHERE r.value IS NOT NULL
      AND r.dataset_name IN ('population', 'gdp_per_capita', 'electricity_prices', 'energy_intensity', 'unemployment_rate')
    GROUP BY r.country_code, CAST(LEFT(r.time_period, 4) AS SMALLINT), r.dataset_name;

    SELECT DISTINCT country_code INTO #countries FROM dbo.staging_bidding_zone;

    SELECT year INTO #years FROM #fig
    UNION SELECT YEAR(timestamp) FROM dbo.staging_entso_actual_load
    UNION SELECT YEAR(timestamp) FROM dbo.staging_entso_forecast
    UNION SELECT YEAR(timestamp) FROM dbo.staging_entso_generation;

    -- one value per country, year and indicator: own year, else latest earlier year
    SELECT c.country_code, y.year, i.indicator,
           COALESCE(f.value, p.value) AS value,
           CASE WHEN f.value IS NULL AND p.value IS NOT NULL
                    THEN CONCAT(i.label, ' from ', p.year)
                WHEN i.indicator = 'electricity_prices' AND f.n_periods = 1
                    THEN CONCAT(i.label, ' from one half-year only')
           END AS note
    INTO #val
    FROM #countries c
    CROSS JOIN #years y
    CROSS JOIN (VALUES ('population', 'population'), ('gdp_per_capita', 'GDP'),
                       ('electricity_prices', 'electricity price'), ('energy_intensity', 'energy intensity'),
                       ('unemployment_rate', 'unemployment')) AS i (indicator, label)
    LEFT JOIN #fig f ON f.country_code = c.country_code AND f.year = y.year AND f.indicator = i.indicator
    OUTER APPLY (SELECT TOP (1) x.value, x.year FROM #fig x
                 WHERE x.country_code = c.country_code AND x.indicator = i.indicator AND x.year < y.year
                 ORDER BY x.year DESC) p;

    SELECT v.country_code, v.year,
           CAST(ROUND(MAX(CASE WHEN v.indicator = 'population'         THEN v.value END), 0) AS BIGINT)        AS population,
           CAST(MAX(CASE WHEN v.indicator = 'gdp_per_capita'           THEN v.value END) AS DECIMAL(12,2))     AS gdp_per_capita_eur,
           CAST(MAX(CASE WHEN v.indicator = 'electricity_prices'       THEN v.value END) AS DECIMAL(8,4))      AS electricity_price_eur_kwh,
           CAST(MAX(CASE WHEN v.indicator = 'energy_intensity'         THEN v.value END) AS DECIMAL(10,2))     AS energy_intensity,
           CAST(MAX(CASE WHEN v.indicator = 'unemployment_rate'        THEN v.value END) AS DECIMAL(5,2))      AS unemployment_rate_pct,
           CASE WHEN COUNT(v.note) > 0 THEN 'Yes' ELSE 'No' END                                                AS is_estimated,
           CAST(STRING_AGG(v.note, '; ') WITHIN GROUP (ORDER BY v.indicator) AS NVARCHAR(200))                 AS estimate_note
    INTO #src
    FROM #val v
    GROUP BY v.country_code, v.year;

    ALTER TABLE #src ADD row_hash BINARY(32);
    UPDATE #src SET row_hash = HASHBYTES('SHA2_256', CONCAT_WS('|',
        ISNULL(CONVERT(VARCHAR(40), population), '~'), ISNULL(CONVERT(VARCHAR(40), gdp_per_capita_eur), '~'),
        ISNULL(CONVERT(VARCHAR(40), electricity_price_eur_kwh), '~'), ISNULL(CONVERT(VARCHAR(40), energy_intensity), '~'),
        ISNULL(CONVERT(VARCHAR(40), unemployment_rate_pct), '~'), is_estimated, ISNULL(estimate_note, '~')));

    BEGIN TRANSACTION;

    -- 1. close the current version where a figure changed (Eurostat revision, or estimate replaced)
    UPDATE h
    SET valid_to = @now, is_current = 'No'
    FROM dbo.dim_socioeconomic_profile_history h
    JOIN #src s ON s.country_code = h.country_code AND s.year = h.year
    WHERE h.is_current = 'Yes' AND h.row_hash <> s.row_hash;

    -- 2. new version for profiles without a current version
    SELECT s.*, k.socioeconomic_profile_key
    INTO #todo
    FROM #src s
    LEFT JOIN (SELECT DISTINCT country_code, year, socioeconomic_profile_key FROM dbo.dim_socioeconomic_profile_history) k
           ON k.country_code = s.country_code AND k.year = s.year
    WHERE NOT EXISTS (SELECT 1 FROM dbo.dim_socioeconomic_profile_history h
                      WHERE h.country_code = s.country_code AND h.year = s.year AND h.is_current = 'Yes');

    DECLARE @max_key INT = (SELECT ISNULL(MAX(socioeconomic_profile_key), 0) FROM dbo.dim_socioeconomic_profile);
    WITH t AS (SELECT socioeconomic_profile_key, ROW_NUMBER() OVER (ORDER BY country_code, year) AS rn
               FROM #todo WHERE socioeconomic_profile_key IS NULL)
    UPDATE t SET socioeconomic_profile_key = @max_key + rn;

    INSERT INTO dbo.dim_socioeconomic_profile_history
        (socioeconomic_profile_key, country_code, year, population, gdp_per_capita_eur, electricity_price_eur_kwh,
         energy_intensity, unemployment_rate_pct, is_estimated, estimate_note, row_hash, valid_from, valid_to, is_current)
    SELECT socioeconomic_profile_key, country_code, year, population, gdp_per_capita_eur, electricity_price_eur_kwh,
           energy_intensity, unemployment_rate_pct, is_estimated, estimate_note, row_hash, @now, '9999-12-31', 'Yes'
    FROM #todo;
    SET @new_versions = @@ROWCOUNT;

    -- 3. the dimension holds the current version of every durable key
    MERGE dbo.dim_socioeconomic_profile AS d
    USING (SELECT * FROM dbo.dim_socioeconomic_profile_history WHERE is_current = 'Yes') AS h
       ON d.socioeconomic_profile_key = h.socioeconomic_profile_key
    WHEN MATCHED AND d.valid_from <> h.valid_from THEN
        UPDATE SET population = h.population, gdp_per_capita_eur = h.gdp_per_capita_eur,
                   electricity_price_eur_kwh = h.electricity_price_eur_kwh, energy_intensity = h.energy_intensity,
                   unemployment_rate_pct = h.unemployment_rate_pct, is_estimated = h.is_estimated,
                   estimate_note = h.estimate_note, valid_from = h.valid_from
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (socioeconomic_profile_key, country_code, year, population, gdp_per_capita_eur, electricity_price_eur_kwh,
                energy_intensity, unemployment_rate_pct, is_estimated, estimate_note, valid_from)
        VALUES (h.socioeconomic_profile_key, h.country_code, h.year, h.population, h.gdp_per_capita_eur,
                h.electricity_price_eur_kwh, h.energy_intensity, h.unemployment_rate_pct, h.is_estimated,
                h.estimate_note, h.valid_from);

    COMMIT TRANSACTION;
END;
GO

-- -------------------------------------------------------------------------------------
-- SSIS task "Load Dimensions"
-- -------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.usp_load_dimensions
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @days INT, @zones INT, @profiles INT, @total INT, @details NVARCHAR(MAX), @error NVARCHAR(MAX),
            @log_id BIGINT;

    EXEC dbo.usp_etl_step_start 'LOAD_DIMENSIONS', @log_id OUTPUT;
    BEGIN TRY
        EXEC dbo.usp_load_dim_date @rows_inserted = @days OUTPUT;
        EXEC dbo.usp_load_dim_bidding_zone @new_versions = @zones OUTPUT;
        EXEC dbo.usp_load_dim_socioeconomic_profile @new_versions = @profiles OUTPUT;

        SET @total = @days + @zones + @profiles;
        SET @details = CONCAT('dim_date: +', @days, ' days; dim_bidding_zone: +', @zones,
                              ' versions; dim_socioeconomic_profile: +', @profiles, ' versions');
        EXEC dbo.usp_etl_step_end @log_id, 'SUCCESS', @records_processed = @total, @execution_details = @details;
        PRINT @details;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
        SET @error = ERROR_MESSAGE();
        EXEC dbo.usp_etl_step_end @log_id, 'FAILED', @error_message = @error;
        THROW;
    END CATCH;
END;
GO

-- -------------------------------------------------------------------------------------
-- Weather per country and hour (decision A): average of the cities that have a value,
-- circular mean for wind direction, most frequent condition (ties: higher WMO code).
-- Averages are exact DECIMAL arithmetic, and the wind components are rounded before ATN2, so the
-- result does not depend on the summation order of a parallel plan (FLOAT averages could round
-- differently between runs, which would make reloads report false changes).
-- -------------------------------------------------------------------------------------
CREATE OR ALTER VIEW dbo.vw_weather_country_hour
AS
WITH agg AS (
    SELECT w.timestamp, w.country_code,
           AVG(w.temperature_avg) AS temperature_c,
           AVG(w.humidity)        AS humidity_pct,
           AVG(w.precipitation)   AS precipitation_mm,
           AVG(w.wind_speed)      AS wind_speed_kmh,
           ROUND(AVG(SIN(RADIANS(CAST(w.wind_direction AS FLOAT)))), 9) AS wind_sin,
           ROUND(AVG(COS(RADIANS(CAST(w.wind_direction AS FLOAT)))), 9) AS wind_cos,
           AVG(w.cloud_cover)     AS cloud_cover_pct,
           AVG(w.solar_radiation) AS solar_radiation_wm2,
           AVG(w.air_pressure)    AS air_pressure_hpa
    FROM dbo.staging_weather_data w
    GROUP BY w.timestamp, w.country_code
), cond AS (
    SELECT w.timestamp, w.country_code, w.weather_condition,
           ROW_NUMBER() OVER (PARTITION BY w.timestamp, w.country_code
                              ORDER BY COUNT(*) DESC, MAX(w.weather_code) DESC) AS rn
    FROM dbo.staging_weather_data w
    WHERE w.weather_condition IS NOT NULL
    GROUP BY w.timestamp, w.country_code, w.weather_condition
)
SELECT a.timestamp, a.country_code, a.temperature_c, a.humidity_pct, a.precipitation_mm, a.wind_speed_kmh,
       -- no mean direction when the cities' winds cancel out (both components 0; ATN2(0, 0) is an error)
       CASE WHEN a.wind_sin IS NULL OR (a.wind_sin = 0 AND a.wind_cos = 0) THEN NULL
            ELSE CAST(ROUND(DEGREES(ATN2(a.wind_sin, a.wind_cos)) + 360, 0) AS INT) % 360 END AS wind_direction_deg,
       a.cloud_cover_pct, a.solar_radiation_wm2, a.air_pressure_hpa,
       c.weather_condition
FROM agg a
LEFT JOIN cond c ON c.timestamp = a.timestamp AND c.country_code = a.country_code AND c.rn = 1;
GO

-- -------------------------------------------------------------------------------------
-- fact_load: one row per zone and hour that has a load or forecast value.
-- Existing rows are updated when a reloaded value differs (ENTSO-E revisions, late data); a stored
-- value is never replaced by NULL and a known key never by 0 (Unknown). New rows are inserted.
-- -------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.usp_load_fact_load
    @rows_inserted INT = 0 OUTPUT,
    @details NVARCHAR(1000) = NULL OUTPUT,
    @rows_updated INT = 0 OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    WITH energy AS (
        SELECT zone_code, timestamp, MAX(actual) AS actual, MAX(forecast) AS forecast
        FROM (SELECT zone_code, timestamp, quantity AS actual, CAST(NULL AS DECIMAL(15,2)) AS forecast
              FROM dbo.staging_entso_actual_load
              UNION ALL
              SELECT zone_code, timestamp, NULL, quantity FROM dbo.staging_entso_forecast) x
        GROUP BY zone_code, timestamp
    )
    SELECT CONVERT(INT, CONVERT(CHAR(8), e.timestamp, 112))       AS date_key,
           CAST(DATEPART(HOUR, e.timestamp) AS TINYINT)           AS time_key,
           ISNULL(z.bidding_zone_key, 0)                          AS bidding_zone_key,
           ISNULL(p.socioeconomic_profile_key, 0)                 AS socioeconomic_profile_key,
           CASE WHEN w.timestamp IS NULL THEN 0
                ELSE ISNULL(c.weather_condition_key, 0) END       AS weather_condition_key,
           CASE WHEN h.holiday_date IS NOT NULL THEN 'Yes' ELSE 'No' END AS is_public_holiday,
           e.actual, e.forecast,
           w.temperature_c, w.humidity_pct, w.precipitation_mm, w.wind_speed_kmh, w.wind_direction_deg,
           w.cloud_cover_pct, w.solar_radiation_wm2, w.air_pressure_hpa,
           CASE WHEN w.temperature_c IS NULL THEN NULL
                WHEN w.temperature_c < 18 THEN (18 - w.temperature_c) / 24 ELSE 0 END AS heating_degree_hours,
           CASE WHEN w.temperature_c IS NULL THEN NULL
                WHEN w.temperature_c > 18 THEN (w.temperature_c - 18) / 24 ELSE 0 END AS cooling_degree_hours
    INTO #src
    FROM energy e
    LEFT JOIN dbo.dim_bidding_zone z         ON z.bidding_zone_code = e.zone_code
    LEFT JOIN dbo.dim_socioeconomic_profile p ON p.country_code = z.country_code AND p.year = YEAR(e.timestamp)
    LEFT JOIN dbo.vw_weather_country_hour w  ON w.country_code = z.country_code AND w.timestamp = e.timestamp
    LEFT JOIN dbo.dim_weather_condition c    ON c.condition_name = w.weather_condition
    LEFT JOIN dbo.ref_public_holiday h       ON h.country_code = z.country_code AND h.holiday_date = CAST(e.timestamp AS DATE);

    BEGIN TRANSACTION;
    UPDATE f
    SET socioeconomic_profile_key = n.socioeconomic_profile_key, weather_condition_key = n.weather_condition_key,
        is_public_holiday = n.is_public_holiday, actual_load_mwh = n.actual_load_mwh,
        forecast_load_mwh = n.forecast_load_mwh, temperature_c = n.temperature_c, humidity_pct = n.humidity_pct,
        precipitation_mm = n.precipitation_mm, wind_speed_kmh = n.wind_speed_kmh,
        wind_direction_deg = n.wind_direction_deg, cloud_cover_pct = n.cloud_cover_pct,
        solar_radiation_wm2 = n.solar_radiation_wm2, air_pressure_hpa = n.air_pressure_hpa,
        heating_degree_hours = n.heating_degree_hours, cooling_degree_hours = n.cooling_degree_hours,
        updated_at = SYSUTCDATETIME()
    FROM dbo.fact_load f
    JOIN #src s ON s.date_key = f.date_key AND s.time_key = f.time_key AND s.bidding_zone_key = f.bidding_zone_key
    CROSS APPLY (SELECT
        CASE WHEN s.socioeconomic_profile_key <> 0 THEN s.socioeconomic_profile_key ELSE f.socioeconomic_profile_key END AS socioeconomic_profile_key,
        CAST(CASE WHEN s.weather_condition_key <> 0 THEN s.weather_condition_key ELSE f.weather_condition_key END AS TINYINT) AS weather_condition_key,
        CAST(s.is_public_holiday AS NVARCHAR(3))                                      AS is_public_holiday,
        ISNULL(CAST(s.actual              AS DECIMAL(12,2)), f.actual_load_mwh)       AS actual_load_mwh,
        ISNULL(CAST(s.forecast            AS DECIMAL(12,2)), f.forecast_load_mwh)     AS forecast_load_mwh,
        ISNULL(CAST(s.temperature_c       AS DECIMAL(5,2)),  f.temperature_c)         AS temperature_c,
        ISNULL(CAST(s.humidity_pct        AS DECIMAL(5,2)),  f.humidity_pct)          AS humidity_pct,
        ISNULL(CAST(s.precipitation_mm    AS DECIMAL(6,2)),  f.precipitation_mm)      AS precipitation_mm,
        ISNULL(CAST(s.wind_speed_kmh      AS DECIMAL(5,2)),  f.wind_speed_kmh)        AS wind_speed_kmh,
        ISNULL(CAST(s.wind_direction_deg  AS SMALLINT),      f.wind_direction_deg)    AS wind_direction_deg,
        ISNULL(CAST(s.cloud_cover_pct     AS DECIMAL(5,2)),  f.cloud_cover_pct)       AS cloud_cover_pct,
        ISNULL(CAST(s.solar_radiation_wm2 AS DECIMAL(7,2)),  f.solar_radiation_wm2)   AS solar_radiation_wm2,
        ISNULL(CAST(s.air_pressure_hpa    AS DECIMAL(7,2)),  f.air_pressure_hpa)      AS air_pressure_hpa,
        ISNULL(CAST(s.heating_degree_hours AS DECIMAL(6,3)), f.heating_degree_hours)  AS heating_degree_hours,
        ISNULL(CAST(s.cooling_degree_hours AS DECIMAL(6,3)), f.cooling_degree_hours)  AS cooling_degree_hours) n
    WHERE EXISTS (SELECT n.socioeconomic_profile_key, n.weather_condition_key, n.is_public_holiday, n.actual_load_mwh,
                         n.forecast_load_mwh, n.temperature_c, n.humidity_pct, n.precipitation_mm, n.wind_speed_kmh,
                         n.wind_direction_deg, n.cloud_cover_pct, n.solar_radiation_wm2, n.air_pressure_hpa,
                         n.heating_degree_hours, n.cooling_degree_hours
                  EXCEPT
                  SELECT f.socioeconomic_profile_key, f.weather_condition_key, f.is_public_holiday, f.actual_load_mwh,
                         f.forecast_load_mwh, f.temperature_c, f.humidity_pct, f.precipitation_mm, f.wind_speed_kmh,
                         f.wind_direction_deg, f.cloud_cover_pct, f.solar_radiation_wm2, f.air_pressure_hpa,
                         f.heating_degree_hours, f.cooling_degree_hours);
    SET @rows_updated = @@ROWCOUNT;

    INSERT INTO dbo.fact_load
        (date_key, time_key, bidding_zone_key, socioeconomic_profile_key, weather_condition_key, is_public_holiday,
         actual_load_mwh, forecast_load_mwh, temperature_c, humidity_pct, precipitation_mm, wind_speed_kmh,
         wind_direction_deg, cloud_cover_pct, solar_radiation_wm2, air_pressure_hpa,
         heating_degree_hours, cooling_degree_hours)
    SELECT s.date_key, s.time_key, s.bidding_zone_key, s.socioeconomic_profile_key, s.weather_condition_key,
           s.is_public_holiday, s.actual, s.forecast, s.temperature_c, s.humidity_pct, s.precipitation_mm,
           s.wind_speed_kmh, s.wind_direction_deg, s.cloud_cover_pct, s.solar_radiation_wm2, s.air_pressure_hpa,
           s.heating_degree_hours, s.cooling_degree_hours
    FROM #src s
    WHERE NOT EXISTS (SELECT 1 FROM dbo.fact_load f
                      WHERE f.date_key = s.date_key AND f.time_key = s.time_key AND f.bidding_zone_key = s.bidding_zone_key);
    SET @rows_inserted = @@ROWCOUNT;
    COMMIT TRANSACTION;

    -- unmatched keys and missing weather in the staged rows (decision G)
    SELECT @details = CONCAT('fact_load: +', @rows_inserted, ' new, ', @rows_updated, ' updated, of ', COUNT(*), ' staged',
                             '; unknown zone: ', ISNULL(SUM(CASE WHEN bidding_zone_key = 0 THEN 1 ELSE 0 END), 0),
                             '; unknown profile: ', ISNULL(SUM(CASE WHEN socioeconomic_profile_key = 0 THEN 1 ELSE 0 END), 0),
                             '; unknown weather condition: ', ISNULL(SUM(CASE WHEN weather_condition_key = 0 THEN 1 ELSE 0 END), 0),
                             '; no weather: ', ISNULL(SUM(CASE WHEN temperature_c IS NULL THEN 1 ELSE 0 END), 0))
    FROM #src;
END;
GO

-- -------------------------------------------------------------------------------------
-- fact_generation: one row per zone, hour and production type; existing rows are updated when a
-- reloaded value differs (never to NULL / Unknown), new rows inserted.
-- Unknown production types share key 0; their values are summed so the grain stays unique. When a
-- reload maps them to a known type, the Unknown row of that zone and hour is replaced.
-- -------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.usp_load_fact_generation
    @rows_inserted INT = 0 OUTPUT,
    @details NVARCHAR(1000) = NULL OUTPUT,
    @rows_updated INT = 0 OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    SELECT CONVERT(INT, CONVERT(CHAR(8), g.timestamp, 112))      AS date_key,
           CAST(DATEPART(HOUR, g.timestamp) AS TINYINT)          AS time_key,
           ISNULL(z.bidding_zone_key, 0)                         AS bidding_zone_key,
           ISNULL(p.socioeconomic_profile_key, 0)                AS socioeconomic_profile_key,
           ISNULL(t.generation_type_key, 0)                      AS generation_type_key,
           MAX(CASE WHEN h.holiday_date IS NOT NULL THEN 'Yes' ELSE 'No' END) AS is_public_holiday,
           SUM(g.quantity)                                       AS generation_mwh,
           COUNT(*)                                              AS n_source_rows
    INTO #src
    FROM dbo.staging_entso_generation g
    LEFT JOIN dbo.dim_bidding_zone z          ON z.bidding_zone_code = g.zone_code
    LEFT JOIN dbo.dim_socioeconomic_profile p ON p.country_code = z.country_code AND p.year = YEAR(g.timestamp)
    LEFT JOIN dbo.dim_generation_type t       ON t.entsoe_code = g.generation_type
    LEFT JOIN dbo.ref_public_holiday h        ON h.country_code = z.country_code AND h.holiday_date = CAST(g.timestamp AS DATE)
    GROUP BY CONVERT(INT, CONVERT(CHAR(8), g.timestamp, 112)), CAST(DATEPART(HOUR, g.timestamp) AS TINYINT),
             ISNULL(z.bidding_zone_key, 0), ISNULL(p.socioeconomic_profile_key, 0), ISNULL(t.generation_type_key, 0);

    DECLARE @rows_reclassified INT;
    BEGIN TRANSACTION;
    -- An Unknown row (key 0) whose production type is known now (e.g. after B25 was added to
    -- dim_generation_type): the reload brings these values in under their real key, so the old
    -- Unknown row is removed. Otherwise the same generation would be counted twice.
    DELETE f
    FROM dbo.fact_generation f
    WHERE f.generation_type_key = 0
      AND EXISTS (SELECT 1 FROM #src s
                  WHERE s.date_key = f.date_key AND s.time_key = f.time_key AND s.bidding_zone_key = f.bidding_zone_key)
      AND NOT EXISTS (SELECT 1 FROM #src s
                      WHERE s.date_key = f.date_key AND s.time_key = f.time_key AND s.bidding_zone_key = f.bidding_zone_key
                        AND s.generation_type_key = 0);
    SET @rows_reclassified = @@ROWCOUNT;

    UPDATE f
    SET socioeconomic_profile_key = n.socioeconomic_profile_key, is_public_holiday = n.is_public_holiday,
        generation_mwh = n.generation_mwh, updated_at = SYSUTCDATETIME()
    FROM dbo.fact_generation f
    JOIN #src s ON s.date_key = f.date_key AND s.time_key = f.time_key
               AND s.bidding_zone_key = f.bidding_zone_key AND s.generation_type_key = f.generation_type_key
    CROSS APPLY (SELECT
        CASE WHEN s.socioeconomic_profile_key <> 0 THEN s.socioeconomic_profile_key ELSE f.socioeconomic_profile_key END AS socioeconomic_profile_key,
        CAST(s.is_public_holiday AS NVARCHAR(3))                             AS is_public_holiday,
        ISNULL(CAST(s.generation_mwh AS DECIMAL(12,2)), f.generation_mwh)    AS generation_mwh) n
    WHERE EXISTS (SELECT n.socioeconomic_profile_key, n.is_public_holiday, n.generation_mwh
                  EXCEPT
                  SELECT f.socioeconomic_profile_key, f.is_public_holiday, f.generation_mwh);
    SET @rows_updated = @@ROWCOUNT;

    INSERT INTO dbo.fact_generation
        (date_key, time_key, bidding_zone_key, socioeconomic_profile_key, generation_type_key, is_public_holiday, generation_mwh)
    SELECT s.date_key, s.time_key, s.bidding_zone_key, s.socioeconomic_profile_key, s.generation_type_key,
           s.is_public_holiday, s.generation_mwh
    FROM #src s
    WHERE NOT EXISTS (SELECT 1 FROM dbo.fact_generation f
                      WHERE f.date_key = s.date_key AND f.time_key = s.time_key
                        AND f.bidding_zone_key = s.bidding_zone_key AND f.generation_type_key = s.generation_type_key);
    SET @rows_inserted = @@ROWCOUNT;
    COMMIT TRANSACTION;

    SELECT @details = CONCAT('fact_generation: +', @rows_inserted, ' new, ', @rows_updated, ' updated, of ', COUNT(*), ' staged',
                             CASE WHEN @rows_reclassified > 0
                                  THEN CONCAT(', ', @rows_reclassified, ' Unknown rows replaced by a known type') END,
                             '; unknown zone: ', ISNULL(SUM(CASE WHEN bidding_zone_key = 0 THEN 1 ELSE 0 END), 0),
                             '; unknown profile: ', ISNULL(SUM(CASE WHEN socioeconomic_profile_key = 0 THEN 1 ELSE 0 END), 0),
                             '; unknown production type: ', ISNULL(SUM(CASE WHEN generation_type_key = 0 THEN 1 ELSE 0 END), 0))
    FROM #src;

    DECLARE @unknown_codes NVARCHAR(200) =
        (SELECT STRING_AGG(code, ',') FROM (SELECT DISTINCT g.generation_type AS code
                                            FROM dbo.staging_entso_generation g
                                            WHERE NOT EXISTS (SELECT 1 FROM dbo.dim_generation_type t
                                                              WHERE t.entsoe_code = g.generation_type)) u);
    IF @unknown_codes IS NOT NULL SET @details = CONCAT(@details, ' (codes: ', @unknown_codes, ')');
END;
GO

-- -------------------------------------------------------------------------------------
-- SSIS task "Load Facts"
-- -------------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.usp_load_facts
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @load INT, @gen INT, @load_upd INT, @gen_upd INT, @load_details NVARCHAR(1000), @gen_details NVARCHAR(1000),
            @details NVARCHAR(MAX), @total INT, @error NVARCHAR(MAX), @log_id BIGINT;

    EXEC dbo.usp_etl_step_start 'LOAD_FACTS', @log_id OUTPUT;
    BEGIN TRY
        EXEC dbo.usp_load_fact_load       @rows_inserted = @load OUTPUT, @details = @load_details OUTPUT, @rows_updated = @load_upd OUTPUT;
        EXEC dbo.usp_load_fact_generation @rows_inserted = @gen  OUTPUT, @details = @gen_details  OUTPUT, @rows_updated = @gen_upd OUTPUT;

        SET @total = @load + @gen + @load_upd + @gen_upd;   -- rows inserted + rows updated
        SET @details = CONCAT(@load_details, ' | ', @gen_details);
        EXEC dbo.usp_etl_step_end @log_id, 'SUCCESS', @records_processed = @total, @execution_details = @details;
        PRINT @details;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
        SET @error = ERROR_MESSAGE();
        EXEC dbo.usp_etl_step_end @log_id, 'FAILED', @error_message = @error;
        THROW;
    END CATCH;
END;
GO

PRINT 'EnergyWeatherDW load procedures are up to date.';
