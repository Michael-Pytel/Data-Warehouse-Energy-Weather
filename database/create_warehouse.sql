-- =====================================================================================
-- EnergyWeatherDW: schema definition (the only source of the warehouse structure)
--
-- Safe to run repeatedly: it creates only what is missing and never drops tables or
-- data. Reference rows (time, generation types, weather conditions, "Unknown" rows)
-- are inserted when missing.
--
-- Design decisions (see docs/PLAN.md):
--   A  weather is one value per country and hour (average of the country's cities)
--   E  yearly Eurostat figures per country and year; SCD Type 2 tracks revisions
--   F  two fact tables: fact_load (zone x hour), fact_generation (zone x hour x type)
--   G  unknown values stay NULL; only keys use the "Unknown" row (key 0)
--   H  schema clean-up (dropped indicators, WMO weather conditions, holidays per country)
--
-- All timestamps are UTC; the grain is one hour.
-- =====================================================================================

IF DB_ID(N'EnergyWeatherDW') IS NULL
    CREATE DATABASE EnergyWeatherDW;
GO

USE EnergyWeatherDW;
GO

-- =====================================================================================
-- STAGING TABLES
-- Written by the Python extract scripts (truncated and refilled on every run).
-- =====================================================================================

-- ENTSO-E (entsoe_client.py); quantity = hourly mean MW = MWh for that hour
IF OBJECT_ID(N'dbo.staging_entso_actual_load', N'U') IS NULL
CREATE TABLE dbo.staging_entso_actual_load (
    id          BIGINT IDENTITY(1,1) PRIMARY KEY,
    timestamp   DATETIME2     NOT NULL,
    quantity    DECIMAL(15,2) NOT NULL,
    zone_code   NVARCHAR(20)  NOT NULL,
    zone_name   NVARCHAR(100) NOT NULL,
    country     NVARCHAR(5)   NOT NULL,
    data_type   NVARCHAR(20)  NOT NULL,
    created_at  DATETIME2     DEFAULT GETDATE()
);

IF OBJECT_ID(N'dbo.staging_entso_generation', N'U') IS NULL
CREATE TABLE dbo.staging_entso_generation (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    timestamp       DATETIME2     NOT NULL,
    quantity        DECIMAL(15,2) NOT NULL,
    zone_code       NVARCHAR(20)  NOT NULL,
    zone_name       NVARCHAR(100) NOT NULL,
    country         NVARCHAR(5)   NOT NULL,
    data_type       NVARCHAR(20)  NOT NULL,
    generation_type NVARCHAR(5)   NULL,
    created_at      DATETIME2     DEFAULT GETDATE()
);

IF OBJECT_ID(N'dbo.staging_entso_forecast', N'U') IS NULL
CREATE TABLE dbo.staging_entso_forecast (
    id          BIGINT IDENTITY(1,1) PRIMARY KEY,
    timestamp   DATETIME2     NOT NULL,
    quantity    DECIMAL(15,2) NOT NULL,
    zone_code   NVARCHAR(20)  NOT NULL,
    zone_name   NVARCHAR(100) NOT NULL,
    country     NVARCHAR(5)   NOT NULL,
    data_type   NVARCHAR(20)  NOT NULL,
    created_at  DATETIME2     DEFAULT GETDATE()
);

-- Open-Meteo hourly weather per city (open_meteo_client.py)
IF OBJECT_ID(N'dbo.staging_weather_data', N'U') IS NULL
CREATE TABLE dbo.staging_weather_data (
    id                BIGINT IDENTITY(1,1) PRIMARY KEY,
    timestamp         DATETIME2     NOT NULL,
    country_code      NVARCHAR(5)   NOT NULL,
    zone_name         NVARCHAR(100) NOT NULL,
    subzone_code      NVARCHAR(10)  NOT NULL,
    subzone_name      NVARCHAR(100) NOT NULL,
    temperature_avg   DECIMAL(5,2),
    humidity          DECIMAL(5,2),
    precipitation     DECIMAL(5,2),
    wind_speed        DECIMAL(5,2),
    wind_direction    INT,
    air_pressure      DECIMAL(7,2),
    cloud_cover       DECIMAL(5,2),
    solar_radiation   DECIMAL(7,2),
    weather_code      INT,
    weather_condition NVARCHAR(30),
    latitude          DECIMAL(10,6),
    longitude         DECIMAL(10,6),
    created_at        DATETIME2 DEFAULT GETDATE()
);

-- Eurostat figures in long format (eurostat_client.py): one row per dataset, country and period
IF OBJECT_ID(N'dbo.staging_eurostat_raw', N'U') IS NULL
CREATE TABLE dbo.staging_eurostat_raw (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    dataset_name    NVARCHAR(100),
    country_code    NVARCHAR(10),
    country_name    NVARCHAR(100),
    time_period     NVARCHAR(20),   -- 'YYYY' or 'YYYY-S1' / 'YYYY-S2' (electricity prices)
    value           FLOAT,
    extraction_date DATETIME DEFAULT GETDATE()
);

-- Bidding zones from etl/zones.py (reference_loader.py), source of dim_bidding_zone
IF OBJECT_ID(N'dbo.staging_bidding_zone', N'U') IS NULL
CREATE TABLE dbo.staging_bidding_zone (
    bidding_zone_code NVARCHAR(20)  NOT NULL PRIMARY KEY,
    bidding_zone_name NVARCHAR(100) NOT NULL,
    country_code      NVARCHAR(5)   NOT NULL,
    country_name      NVARCHAR(100) NOT NULL,
    timezone          NVARCHAR(40)  NOT NULL
);
GO

-- =====================================================================================
-- REFERENCE TABLES
-- =====================================================================================

-- Public holidays per country (generated with the Python 'holidays' package, decision H3).
-- Power BI cannot join on country + date, so the fact load copies the flag into is_public_holiday.
IF OBJECT_ID(N'dbo.ref_public_holiday', N'U') IS NULL
CREATE TABLE dbo.ref_public_holiday (
    country_code  NVARCHAR(5)   NOT NULL,
    holiday_date  DATE          NOT NULL,
    holiday_name  NVARCHAR(200) NOT NULL,   -- several holidays on one date are joined with '; '
    CONSTRAINT PK_ref_public_holiday PRIMARY KEY (country_code, holiday_date)
);
GO

-- =====================================================================================
-- DIMENSIONS: fixed
-- =====================================================================================

-- Calendar day. date_key = yyyymmdd. Facts always match (keys are calculated from the
-- timestamp and the load fills the calendar first), so there is no "Unknown" row.
IF OBJECT_ID(N'dbo.dim_date', N'U') IS NULL
CREATE TABLE dbo.dim_date (
    date_key      INT          NOT NULL CONSTRAINT PK_dim_date PRIMARY KEY,
    full_date     DATE         NOT NULL CONSTRAINT UQ_dim_date_full_date UNIQUE,
    year          SMALLINT     NOT NULL,
    quarter       TINYINT      NOT NULL,
    month         TINYINT      NOT NULL,
    month_name    NVARCHAR(10) NOT NULL,
    year_month    CHAR(7)      NOT NULL,   -- 'YYYY-MM'
    day_of_month  TINYINT      NOT NULL,
    day_of_week   TINYINT      NOT NULL,   -- 1 = Monday ... 7 = Sunday (ISO)
    day_name      NVARCHAR(10) NOT NULL,
    is_weekend    NVARCHAR(3)  NOT NULL,   -- 'Yes' / 'No'
    season        NVARCHAR(10) NOT NULL    -- meteorological: Winter = Dec-Feb, ...
);

-- Hour of the day in UTC (decision H4: no local peak hours). time_key = hour 0-23.
IF OBJECT_ID(N'dbo.dim_time', N'U') IS NULL
CREATE TABLE dbo.dim_time (
    time_key    TINYINT NOT NULL CONSTRAINT PK_dim_time PRIMARY KEY,
    hour_label  CHAR(5) NOT NULL   -- '00:00' ... '23:00' (UTC)
);

-- ENTSO-E production types (psrType). Key 0 = Unknown.
IF OBJECT_ID(N'dbo.dim_generation_type', N'U') IS NULL
CREATE TABLE dbo.dim_generation_type (
    generation_type_key  TINYINT      NOT NULL CONSTRAINT PK_dim_generation_type PRIMARY KEY,
    entsoe_code          NVARCHAR(5)  NOT NULL CONSTRAINT UQ_dim_generation_type_code UNIQUE,
    generation_type_name NVARCHAR(50) NOT NULL,
    generation_category  NVARCHAR(20) NOT NULL,   -- Renewable, Fossil, Nuclear, Storage, Other
    is_renewable         NVARCHAR(3)  NOT NULL,
    is_intermittent      NVARCHAR(3)  NOT NULL
);

-- Weather condition per country and hour: the most frequent WMO-based condition among the
-- country's cities (decisions A, H2). Key 0 = Unknown.
IF OBJECT_ID(N'dbo.dim_weather_condition', N'U') IS NULL
CREATE TABLE dbo.dim_weather_condition (
    weather_condition_key TINYINT      NOT NULL CONSTRAINT PK_dim_weather_condition PRIMARY KEY,
    condition_name        NVARCHAR(30) NOT NULL CONSTRAINT UQ_dim_weather_condition_name UNIQUE,
    severity              NVARCHAR(10) NOT NULL,   -- Mild, Moderate, Severe
    is_extreme            NVARCHAR(3)  NOT NULL
);
GO

-- =====================================================================================
-- DIMENSIONS: SCD Type 2 (decision E)
--
-- Each one is two tables:
--   *_history  every version of a business key (row_hash, valid_from, valid_to, is_current);
--              valid_from / valid_to = when the version was loaded
--   dim_*      one row per durable key with the current version; facts point here.
-- The durable key never changes, so facts don't need updating when a new version arrives.
-- The load procedure (step 2.2) keeps both tables in sync. Key 0 = Unknown.
-- =====================================================================================

-- Bidding zone: descriptive attributes only (no yearly figures)
IF OBJECT_ID(N'dbo.dim_bidding_zone', N'U') IS NULL
CREATE TABLE dbo.dim_bidding_zone (
    bidding_zone_key  INT           NOT NULL CONSTRAINT PK_dim_bidding_zone PRIMARY KEY,
    bidding_zone_code NVARCHAR(20)  NOT NULL CONSTRAINT UQ_dim_bidding_zone_code UNIQUE,   -- ENTSO-E EIC code
    bidding_zone_name NVARCHAR(100) NOT NULL,
    country_code      NVARCHAR(5)   NOT NULL,
    country_name      NVARCHAR(100) NOT NULL,
    timezone          NVARCHAR(40)  NOT NULL,
    valid_from        DATETIME2(0)  NOT NULL   -- load time of the current version
);

IF OBJECT_ID(N'dbo.dim_bidding_zone_history', N'U') IS NULL
CREATE TABLE dbo.dim_bidding_zone_history (
    bidding_zone_version_id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_dim_bidding_zone_history PRIMARY KEY,
    bidding_zone_key        INT           NOT NULL,
    bidding_zone_code       NVARCHAR(20)  NOT NULL,
    bidding_zone_name       NVARCHAR(100) NOT NULL,
    country_code            NVARCHAR(5)   NOT NULL,
    country_name            NVARCHAR(100) NOT NULL,
    timezone                NVARCHAR(40)  NOT NULL,
    row_hash                BINARY(32)    NOT NULL,   -- SHA2_256 of all descriptive attributes
    valid_from              DATETIME2(0)  NOT NULL,
    valid_to                DATETIME2(0)  NOT NULL CONSTRAINT DF_dim_bidding_zone_history_valid_to DEFAULT '9999-12-31',
    is_current              NVARCHAR(3)   NOT NULL,   -- 'Yes' / 'No'
    CONSTRAINT UQ_dim_bidding_zone_history_version UNIQUE (bidding_zone_code, valid_from)
);

-- Yearly figures per country and year (Eurostat). Durable key = one per country + year.
-- For a year Eurostat hasn't published yet, the figures of the latest published year are used
-- and is_estimated = 'Yes' (estimate_note says which figures). Missing figures stay NULL (decision G).
IF OBJECT_ID(N'dbo.dim_socioeconomic_profile', N'U') IS NULL
CREATE TABLE dbo.dim_socioeconomic_profile (
    socioeconomic_profile_key INT           NOT NULL CONSTRAINT PK_dim_socioeconomic_profile PRIMARY KEY,
    country_code              NVARCHAR(5)   NOT NULL,
    year                      SMALLINT      NULL,      -- NULL only on the Unknown row
    population                BIGINT        NULL,
    gdp_per_capita_eur        DECIMAL(12,2) NULL,      -- current prices, EUR per person
    electricity_price_eur_kwh DECIMAL(8,4)  NULL,      -- households, band DC, excl. taxes; mean of S1/S2
    energy_intensity          DECIMAL(10,2) NULL,      -- sdg_07_30
    unemployment_rate_pct     DECIMAL(5,2)  NULL,      -- age 15-74
    is_estimated              NVARCHAR(3)   NOT NULL,  -- 'Yes' if any figure comes from an earlier year
    estimate_note             NVARCHAR(200) NULL,
    valid_from                DATETIME2(0)  NOT NULL,
    CONSTRAINT UQ_dim_socioeconomic_profile_country_year UNIQUE (country_code, year)
);

IF OBJECT_ID(N'dbo.dim_socioeconomic_profile_history', N'U') IS NULL
CREATE TABLE dbo.dim_socioeconomic_profile_history (
    socioeconomic_profile_version_id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_dim_socioeconomic_profile_history PRIMARY KEY,
    socioeconomic_profile_key        INT           NOT NULL,
    country_code                     NVARCHAR(5)   NOT NULL,
    year                             SMALLINT      NOT NULL,
    population                       BIGINT        NULL,
    gdp_per_capita_eur               DECIMAL(12,2) NULL,
    electricity_price_eur_kwh        DECIMAL(8,4)  NULL,
    energy_intensity                 DECIMAL(10,2) NULL,
    unemployment_rate_pct            DECIMAL(5,2)  NULL,
    is_estimated                     NVARCHAR(3)   NOT NULL,
    estimate_note                    NVARCHAR(200) NULL,
    row_hash                         BINARY(32)    NOT NULL,
    valid_from                       DATETIME2(0)  NOT NULL,
    valid_to                         DATETIME2(0)  NOT NULL CONSTRAINT DF_dim_socioeconomic_profile_history_valid_to DEFAULT '9999-12-31',
    is_current                       NVARCHAR(3)   NOT NULL,
    CONSTRAINT UQ_dim_socioeconomic_profile_history_version UNIQUE (country_code, year, valid_from)
);
GO

-- =====================================================================================
-- FACTS (decision F): base values only; ratios are calculated in the report.
-- Measures are NULL when the source has no value (decision G). Insert-only: the primary
-- key on the grain prevents duplicates.
-- =====================================================================================

-- Load, forecast and weather per bidding zone and hour
IF OBJECT_ID(N'dbo.fact_load', N'U') IS NULL
CREATE TABLE dbo.fact_load (
    date_key                  INT          NOT NULL,
    time_key                  TINYINT      NOT NULL,
    bidding_zone_key          INT          NOT NULL CONSTRAINT DF_fact_load_zone DEFAULT 0,
    socioeconomic_profile_key INT          NOT NULL CONSTRAINT DF_fact_load_profile DEFAULT 0,
    weather_condition_key     TINYINT      NOT NULL CONSTRAINT DF_fact_load_condition DEFAULT 0,
    is_public_holiday         NVARCHAR(3)  NOT NULL,   -- 'Yes' / 'No' in the zone's country
    actual_load_mwh           DECIMAL(12,2) NULL,
    forecast_load_mwh         DECIMAL(12,2) NULL,      -- day-ahead total load forecast
    -- weather of the zone's country: average of its cities (decision A)
    temperature_c             DECIMAL(5,2) NULL,
    humidity_pct              DECIMAL(5,2) NULL,
    precipitation_mm          DECIMAL(6,2) NULL,
    wind_speed_kmh            DECIMAL(5,2) NULL,
    wind_direction_deg        SMALLINT     NULL,       -- circular mean of the cities
    cloud_cover_pct           DECIMAL(5,2) NULL,
    solar_radiation_wm2       DECIMAL(7,2) NULL,       -- shortwave radiation, mean of the preceding hour
    air_pressure_hpa          DECIMAL(7,2) NULL,
    heating_degree_hours      DECIMAL(6,3) NULL,       -- max(18 - temperature, 0) / 24; sums to degree-days
    cooling_degree_hours      DECIMAL(6,3) NULL,       -- max(temperature - 18, 0) / 24
    loaded_at                 DATETIME2(0) NOT NULL CONSTRAINT DF_fact_load_loaded_at DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_fact_load PRIMARY KEY (date_key, time_key, bidding_zone_key),
    CONSTRAINT FK_fact_load_date      FOREIGN KEY (date_key)                  REFERENCES dbo.dim_date (date_key),
    CONSTRAINT FK_fact_load_time      FOREIGN KEY (time_key)                  REFERENCES dbo.dim_time (time_key),
    CONSTRAINT FK_fact_load_zone      FOREIGN KEY (bidding_zone_key)          REFERENCES dbo.dim_bidding_zone (bidding_zone_key),
    CONSTRAINT FK_fact_load_profile   FOREIGN KEY (socioeconomic_profile_key) REFERENCES dbo.dim_socioeconomic_profile (socioeconomic_profile_key),
    CONSTRAINT FK_fact_load_condition FOREIGN KEY (weather_condition_key)     REFERENCES dbo.dim_weather_condition (weather_condition_key)
);

-- Generation per bidding zone, hour and production type
IF OBJECT_ID(N'dbo.fact_generation', N'U') IS NULL
CREATE TABLE dbo.fact_generation (
    date_key                  INT           NOT NULL,
    time_key                  TINYINT       NOT NULL,
    bidding_zone_key          INT           NOT NULL CONSTRAINT DF_fact_generation_zone DEFAULT 0,
    socioeconomic_profile_key INT           NOT NULL CONSTRAINT DF_fact_generation_profile DEFAULT 0,
    generation_type_key       TINYINT       NOT NULL CONSTRAINT DF_fact_generation_type DEFAULT 0,
    is_public_holiday         NVARCHAR(3)   NOT NULL,
    generation_mwh            DECIMAL(12,2) NULL,
    loaded_at                 DATETIME2(0)  NOT NULL CONSTRAINT DF_fact_generation_loaded_at DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_fact_generation PRIMARY KEY (date_key, time_key, bidding_zone_key, generation_type_key),
    CONSTRAINT FK_fact_generation_date    FOREIGN KEY (date_key)                  REFERENCES dbo.dim_date (date_key),
    CONSTRAINT FK_fact_generation_time    FOREIGN KEY (time_key)                  REFERENCES dbo.dim_time (time_key),
    CONSTRAINT FK_fact_generation_zone    FOREIGN KEY (bidding_zone_key)          REFERENCES dbo.dim_bidding_zone (bidding_zone_key),
    CONSTRAINT FK_fact_generation_profile FOREIGN KEY (socioeconomic_profile_key) REFERENCES dbo.dim_socioeconomic_profile (socioeconomic_profile_key),
    CONSTRAINT FK_fact_generation_type    FOREIGN KEY (generation_type_key)       REFERENCES dbo.dim_generation_type (generation_type_key)
);
GO

-- Indexes for filtering by zone (the primary keys already start with date and hour)
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_fact_load_zone' AND object_id = OBJECT_ID(N'dbo.fact_load'))
    CREATE INDEX IX_fact_load_zone ON dbo.fact_load (bidding_zone_key, date_key);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_fact_generation_zone' AND object_id = OBJECT_ID(N'dbo.fact_generation'))
    CREATE INDEX IX_fact_generation_zone ON dbo.fact_generation (bidding_zone_key, date_key);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_dim_bidding_zone_history_current' AND object_id = OBJECT_ID(N'dbo.dim_bidding_zone_history'))
    CREATE UNIQUE INDEX IX_dim_bidding_zone_history_current ON dbo.dim_bidding_zone_history (bidding_zone_key) WHERE is_current = 'Yes';
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_dim_socioeconomic_profile_history_current' AND object_id = OBJECT_ID(N'dbo.dim_socioeconomic_profile_history'))
    CREATE UNIQUE INDEX IX_dim_socioeconomic_profile_history_current ON dbo.dim_socioeconomic_profile_history (socioeconomic_profile_key) WHERE is_current = 'Yes';
GO

-- =====================================================================================
-- ETL LOG (step 2.3): one table for the package run and every step in it
--   process_name 'ETL_RUN' = one SSIS package run; the steps of that run point to it with run_id
--   (run_id is NULL when a script is started by hand outside the package).
--   status: RUNNING -> SUCCESS | FAILED; rows left RUNNING by a crashed run become ABANDONED
--   at the start of the next run. Times are local server time.
--   Steps:  usp_etl_step_start / usp_etl_step_end  (Python helper etl/etl_log.py, load procedures)
--   Run:    usp_etl_run_start / usp_etl_run_end    (SSIS tasks "Start Run" / "Finish Run", OnError handler)
-- =====================================================================================

IF OBJECT_ID(N'dbo.etl_log', N'U') IS NULL
CREATE TABLE dbo.etl_log (
    log_id            BIGINT IDENTITY(1,1) PRIMARY KEY,
    process_name      NVARCHAR(100) NOT NULL,
    start_time        DATETIME2     NOT NULL,
    end_time          DATETIME2,
    status            NVARCHAR(20)  NOT NULL,
    records_processed INT DEFAULT 0,
    error_message     NVARCHAR(MAX),
    execution_details NVARCHAR(MAX),
    run_id            BIGINT NULL
);
GO

IF COL_LENGTH(N'dbo.etl_log', N'run_id') IS NULL
    ALTER TABLE dbo.etl_log ADD run_id BIGINT NULL;
GO

DROP PROCEDURE IF EXISTS dbo.sp_log_etl_process;   -- replaced by the procedures below
GO

-- Start of one step; returns its log_id. Linked to the package run in progress, if any.
CREATE OR ALTER PROCEDURE dbo.usp_etl_step_start
    @process_name NVARCHAR(100),
    @log_id       BIGINT OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @run_id BIGINT = (SELECT MAX(log_id) FROM dbo.etl_log
                              WHERE process_name = 'ETL_RUN' AND status = 'RUNNING');
    INSERT INTO dbo.etl_log (process_name, start_time, status, records_processed, run_id)
    VALUES (@process_name, SYSDATETIME(), 'RUNNING', 0, @run_id);
    SET @log_id = SCOPE_IDENTITY();
END;
GO

-- End of one step ('SUCCESS' or 'FAILED'); only a row that is still RUNNING is changed.
CREATE OR ALTER PROCEDURE dbo.usp_etl_step_end
    @log_id            BIGINT,
    @status            NVARCHAR(20),
    @records_processed INT = 0,
    @execution_details NVARCHAR(MAX) = NULL,
    @error_message     NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE dbo.etl_log
    SET end_time = SYSDATETIME(), status = @status, records_processed = ISNULL(@records_processed, 0),
        execution_details = @execution_details, error_message = @error_message
    WHERE log_id = @log_id AND status = 'RUNNING';
END;
GO

-- SSIS task "Start Run": stops if another run is in progress (started less than 2 hours ago),
-- closes rows left RUNNING by crashed runs as ABANDONED, and returns the new run's log_id.
CREATE OR ALTER PROCEDURE dbo.usp_etl_run_start
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM dbo.etl_log
               WHERE process_name = 'ETL_RUN' AND status = 'RUNNING'
                 AND start_time > DATEADD(HOUR, -2, SYSDATETIME()))
        THROW 50001, 'Another ETL run is in progress (ETL_RUN row with status RUNNING, started less than 2 hours ago). If it crashed, close it with: EXEC dbo.usp_etl_run_end <log_id>, ''ABANDONED''', 1;

    UPDATE dbo.etl_log
    SET end_time = SYSDATETIME(), status = 'ABANDONED',
        error_message = 'Not finished (process stopped); closed at the start of the next run'
    WHERE status = 'RUNNING';

    DECLARE @log_id BIGINT;
    INSERT INTO dbo.etl_log (process_name, start_time, status, records_processed)
    VALUES ('ETL_RUN', SYSDATETIME(), 'RUNNING', 0);
    SET @log_id = SCOPE_IDENTITY();
    UPDATE dbo.etl_log SET run_id = @log_id WHERE log_id = @log_id;

    SELECT CAST(@log_id AS INT) AS run_id;   -- INT for the SSIS variable User::RunId
END;
GO

-- SSIS task "Finish Run" ('SUCCESS') and the package OnError handler ('FAILED').
-- Summarises the steps of the run (records_processed = number of steps, execution_details = their
-- status); steps still RUNNING are closed with the same status.
-- Does nothing when the run is no longer RUNNING (OnError can fire several times) or @run_id = 0.
CREATE OR ALTER PROCEDURE dbo.usp_etl_run_end
    @run_id BIGINT,
    @status NVARCHAR(20)
AS
BEGIN
    SET NOCOUNT ON;
    IF NOT EXISTS (SELECT 1 FROM dbo.etl_log WHERE log_id = @run_id AND process_name = 'ETL_RUN' AND status = 'RUNNING')
        RETURN;

    UPDATE dbo.etl_log
    SET end_time = SYSDATETIME(), status = @status,
        error_message = 'Still running when the package run ended'
    WHERE run_id = @run_id AND log_id <> @run_id AND status = 'RUNNING';

    DECLARE @details NVARCHAR(MAX), @failed NVARCHAR(MAX);
    SELECT @details = STRING_AGG(CONCAT(process_name, ' ', status), '; ') WITHIN GROUP (ORDER BY log_id),
           @failed  = STRING_AGG(CASE WHEN status <> 'SUCCESS' THEN process_name END, ', ')
    FROM dbo.etl_log
    WHERE run_id = @run_id AND log_id <> @run_id;

    UPDATE dbo.etl_log
    SET end_time = SYSDATETIME(), status = @status,
        records_processed = (SELECT COUNT(*) FROM dbo.etl_log WHERE run_id = @run_id AND log_id <> @run_id),
        execution_details = @details,
        error_message = CASE WHEN @status <> 'SUCCESS' THEN CONCAT('Failed or unfinished steps: ', ISNULL(@failed, 'none (failed before the first step)')) END
    WHERE log_id = @run_id;
END;
GO

-- =====================================================================================
-- REFERENCE ROWS (inserted when missing; existing rows are never changed)
-- =====================================================================================

-- 24 UTC hours
INSERT INTO dbo.dim_time (time_key, hour_label)
SELECT h.n, RIGHT('0' + CAST(h.n AS VARCHAR(2)), 2) + ':00'
FROM (SELECT TOP (24) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS n FROM sys.all_objects) AS h
WHERE NOT EXISTS (SELECT 1 FROM dbo.dim_time t WHERE t.time_key = h.n);

-- ENTSO-E production types (B10 pumped storage only shifts energy, so it is not renewable)
INSERT INTO dbo.dim_generation_type
    (generation_type_key, entsoe_code, generation_type_name, generation_category, is_renewable, is_intermittent)
SELECT v.k, v.code, v.name, v.category, v.renewable, v.intermittent
FROM (VALUES
    ( 0, N'UNK', N'Unknown',                         N'Unknown',   N'No',  N'No'),
    ( 1, N'B01', N'Biomass',                         N'Renewable', N'Yes', N'No'),
    ( 2, N'B02', N'Fossil Brown coal/Lignite',       N'Fossil',    N'No',  N'No'),
    ( 3, N'B03', N'Fossil Coal-derived gas',         N'Fossil',    N'No',  N'No'),
    ( 4, N'B04', N'Fossil Gas',                      N'Fossil',    N'No',  N'No'),
    ( 5, N'B05', N'Fossil Hard coal',                N'Fossil',    N'No',  N'No'),
    ( 6, N'B06', N'Fossil Oil',                      N'Fossil',    N'No',  N'No'),
    ( 7, N'B07', N'Fossil Oil shale',                N'Fossil',    N'No',  N'No'),
    ( 8, N'B08', N'Fossil Peat',                     N'Fossil',    N'No',  N'No'),
    ( 9, N'B09', N'Geothermal',                      N'Renewable', N'Yes', N'No'),
    (10, N'B10', N'Hydro Pumped Storage',            N'Storage',   N'No',  N'No'),
    (11, N'B11', N'Hydro Run-of-river and poundage', N'Renewable', N'Yes', N'Yes'),
    (12, N'B12', N'Hydro Water Reservoir',           N'Renewable', N'Yes', N'No'),
    (13, N'B13', N'Marine',                          N'Renewable', N'Yes', N'Yes'),
    (14, N'B14', N'Nuclear',                         N'Nuclear',   N'No',  N'No'),
    (15, N'B15', N'Other renewable',                 N'Renewable', N'Yes', N'No'),
    (16, N'B16', N'Solar',                           N'Renewable', N'Yes', N'Yes'),
    (17, N'B17', N'Waste',                           N'Other',     N'No',  N'No'),
    (18, N'B18', N'Wind Offshore',                   N'Renewable', N'Yes', N'Yes'),
    (19, N'B19', N'Wind Onshore',                    N'Renewable', N'Yes', N'Yes'),
    (20, N'B20', N'Other',                           N'Other',     N'No',  N'No')
) AS v (k, code, name, category, renewable, intermittent)
WHERE NOT EXISTS (SELECT 1 FROM dbo.dim_generation_type g WHERE g.generation_type_key = v.k);

-- Weather conditions produced by open_meteo_client.py (WMO_CONDITIONS)
INSERT INTO dbo.dim_weather_condition (weather_condition_key, condition_name, severity, is_extreme)
SELECT v.k, v.name, v.severity, v.extreme
FROM (VALUES
    (0, N'Unknown',       N'Unknown',  N'No'),
    (1, N'Clear',         N'Mild',     N'No'),
    (2, N'Partly Cloudy', N'Mild',     N'No'),
    (3, N'Overcast',      N'Mild',     N'No'),
    (4, N'Fog',           N'Mild',     N'No'),
    (5, N'Rain',          N'Moderate', N'No'),
    (6, N'Heavy Rain',    N'Severe',   N'Yes'),
    (7, N'Snow',          N'Moderate', N'No'),
    (8, N'Heavy Snow',    N'Severe',   N'Yes'),
    (9, N'Storm',         N'Severe',   N'Yes')
) AS v (k, name, severity, extreme)
WHERE NOT EXISTS (SELECT 1 FROM dbo.dim_weather_condition c WHERE c.weather_condition_key = v.k);

-- "Unknown" rows of the SCD2 dimensions (key 0)
IF NOT EXISTS (SELECT 1 FROM dbo.dim_bidding_zone WHERE bidding_zone_key = 0)
    INSERT INTO dbo.dim_bidding_zone (bidding_zone_key, bidding_zone_code, bidding_zone_name, country_code, country_name, timezone, valid_from)
    VALUES (0, N'UNKNOWN', N'Unknown', N'UNK', N'Unknown', N'Unknown', '1900-01-01');

IF NOT EXISTS (SELECT 1 FROM dbo.dim_socioeconomic_profile WHERE socioeconomic_profile_key = 0)
    INSERT INTO dbo.dim_socioeconomic_profile (socioeconomic_profile_key, country_code, year, is_estimated, valid_from)
    VALUES (0, N'UNK', NULL, N'No', '1900-01-01');
GO

PRINT 'EnergyWeatherDW schema is up to date.';
