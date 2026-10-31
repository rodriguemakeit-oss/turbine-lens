-- Recovered verbatim from the live DBSQL-managed streaming table with
-- `SHOW CREATE TABLE`, then adapted for a Lakeflow declarative pipeline:
--   * the fully-qualified name becomes a BARE name — a pipeline publishes to the
--     catalog and schema in its own configuration, so hardcoding them here would
--     defeat per-target deployment;
--   * `CREATE STREAMING TABLE` becomes `CREATE OR REFRESH STREAMING TABLE`, the
--     idiomatic form for a pipeline dataset;
--   * the `SCHEDULE CRON` clause is REMOVED. That clause is a DBSQL feature and has
--     no meaning in a pipeline; scheduling lives on the pipeline trigger instead
--     (see resources/asset_api.pipeline.yml), which also makes it visible in the bundle
--     rather than buried in table DDL.
-- Everything else — every column, type, comment and every schemaHint — is unchanged,
-- because the whole point is a table identical to the one this replaces.
--
-- `FROM STREAM read_files(...)`, not `FROM read_files(...)`: without STREAM this is a
-- batch query and a streaming table cannot be built from one.

CREATE OR REFRESH STREAMING TABLE onm_kpis_loadback_all (
  kpi_cd STRING COLLATE UTF8_BINARY COMMENT 'KPI code identifier. Prefix indicates the metric family: PGEN (electricity generation and unavailability), PCAP (capacity in MW), PFUEL (fuel consumption in GJLHV), PCO2/PGHG (emissions in tonnes), PBES (BESS battery metrics). Examples: PGEN0010 = Net electricity generated (MWh), PGEN0099 = Internal unavailable electrical energy (MWh), PGEN0100 = Planned unavailable energy (MWh), PGEN0110 = Unplanned unavailable energy (MWh), PGEN0130 = Forced unplanned, PGEN0140 = External/curtailment, PGEN0150 = Planned unavailable time (h), PCAP0010 = Net max electrical capacity in operation (MW).',
  perform_unit_id STRING COLLATE UTF8_BINARY COMMENT 'Unique identifier of the energy production unit. Foreign key to master_data.perform_unit_id. Always join on this column to enrich KPI data with asset attributes such as technology, region, business unit, and operational status.',
  period STRING COLLATE UTF8_BINARY COMMENT 'String label of the reporting period. Format varies by granularity: monthly (e.g. M2024-01), quarterly (e.g. Q2024-Q1), or annual. Use period_date for date arithmetic and range filters.',
  period_date DATE COMMENT 'Start date of the reporting period (DATE type). Use this column for date range filters and time-series analysis. Aligns with year, month, and quarter columns for aggregations.',
  year BIGINT COMMENT 'Calendar year of the reporting period (e.g. 2023, 2024). Use for annual aggregations or year-over-year comparisons.',
  month BIGINT COMMENT 'Calendar month number (1 to 12) of the reporting period. Null for quarterly or annual records.',
  quarter STRING COLLATE UTF8_BINARY COMMENT 'Quarter label of the reporting period (e.g. Q1, Q2, Q3, Q4). Null for monthly records.',
  value DOUBLE COMMENT 'Numeric KPI measurement. The unit of measure is in the unit column. This is the primary metric column to aggregate — use SUM for energy and time volumes, AVG for performance rates. Always interpret alongside kpi_cd and unit.',
  consolidation_type STRING COLLATE UTF8_BINARY COMMENT 'Consolidation scope for group reporting. Filter to a consistent value when comparing across units or aggregating to group level to avoid double-counting.',
  extract_date_tran DATE COMMENT 'Date when this record was extracted or loaded from the source system. Useful for identifying data freshness or filtering to a specific loadback snapshot.',
  kpi_description STRING COLLATE UTF8_BINARY COMMENT 'Human-readable description of the KPI measured by kpi_cd. Use for display or to search KPIs by keyword (e.g. kpi_description LIKE \'%unavail%\').',
  unit STRING COLLATE UTF8_BINARY COMMENT 'Unit of measure for the value column. Key values: MWh (energy), h (time), MW (capacity), % (rate), t Tonnes (emissions), GJLHV (fuel energy), Number of (event counts).',
  version STRING COLLATE UTF8_BINARY COMMENT 'Data version or loadback identifier. Always filter to a specific version for consistent analysis and to avoid double-counting across versions.',
  _rescued_data STRING COLLATE UTF8_BINARY COMMENT 'Auto Loader internal column. Contains raw source data that could not be parsed into the schema. Non-null values signal data quality issues.')
COMMENT 'Time-series KPI fact table for operational performance monitoring of energy production and storage units. Each row is one KPI value for one unit in one reporting period. Always join with master_data on perform_unit_id to enrich with asset attributes (technology, region, business unit, status). KPI families — PGEN: electricity generation and unavailability (MWh, hours); PCAP: installed capacity (MW); PFUEL: fuel consumption (GJLHV); PCO2/PGHG: emissions (tonnes CO2eq); PBES: battery storage metrics. Key unavailability KPIs: PGEN0099 = total internal unavailable energy (MWh), PGEN0100 = planned unavailable energy, PGEN0110 = unplanned unavailable energy, PGEN0130 = forced unplanned, PGEN0140 = external/curtailment, PGEN0150 = planned unavailable time (h), PGEN0180 = unplanned unavailable time (h), PGEN0190 = external unavailable time (h). Always filter on version and consolidation_type for consistent views and to avoid double-counting.'
AS select `kpi_cd` as `kpi_cd`, `perform_unit_id` as `perform_unit_id`, `period` as `period`, `period_date` as `period_date`, `year` as `year`, `month` as `month`, `quarter` as `quarter`, `value` as `value`, `consolidation_type` as `consolidation_type`, `extract_date_tran` as `extract_date_tran`, `kpi_description` as `kpi_description`, `unit` as `unit`, `version` as `version`, `_rescued_data` as `_rescued_data` from stream read_files('${asset_api.source_root}/onm_kpis_loadback_all', schemaHints => '`kpi_cd` string, `perform_unit_id` string, `period` string, `period_date` date, `year` bigint, `month` bigint, `quarter` string, `value` double, `consolidation_type` string, `extract_date_tran` date, `kpi_description` string, `unit` string, `version` string, `_rescued_data` string')
;
