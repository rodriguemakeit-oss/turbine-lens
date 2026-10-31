-- What the `fleet_reporting` Genie space ("Datasquare Fleet perf KPIs") reads.
--
-- Sibling datasets are referenced by BARE NAME, not `turbine_lens_dev.asset_api.x`: a
-- pipeline publishes into the catalog and schema from its own configuration, so a
-- fully-qualified reference would pin every environment to noprod.
--
-- The two `MAX(...)` subqueries are load-bearing and must stay. The source tables are
-- append-only (Auto Loader adds every extract's rows), so "latest extract" is a filter,
-- not a property of the table. Introducing CDC to make them unnecessary would be wrong
-- here — see the note in v_wind_assets.sql, which ranks multiple rows per unit inside a
-- single extract.
--
-- ~50 of several hundred KPI codes. The groupings below are the only record of which
-- codes matter and why; Databricks strips `--` comments from a stored view definition,
-- so this file is the source of truth, not `information_schema.views`.
-- Declared as a plain VIEW, matching the original: computed on access, so it always
-- reflects the latest extract with no refresh to schedule. In a pipeline this publishes
-- a persistent view to Unity Catalog.
--
-- Worth knowing, not silently changed: `CREATE OR REFRESH MATERIALIZED VIEW` would
-- store the result instead, making Genie's queries far cheaper — v_wind_kpis reduces
-- ~775M rows to ~5.1M, and every question currently re-runs that reduction, including
-- two MAX() subqueries over the full table. The cost is staleness between refreshes.
-- That is a real trade and belongs to whoever owns the data, so the view stays a view.
CREATE VIEW v_wind_kpis AS
SELECT
  kpi_cd,
  kpi_description,
  unit           AS kpi_unit,
  perform_unit_id,
  version,
  period,
  period_date,
  YEAR(period_date)  AS year_number,
  MONTH(period_date) AS month_number,
  quarter,
  value,
  extract_date_tran
FROM onm_kpis_loadback_all
WHERE consolidation_type = 'FULL'
  AND extract_date_tran = (SELECT MAX(extract_date_tran)
                           FROM onm_kpis_loadback_all)
  AND perform_unit_id IN (
      SELECT perform_unit_id
      FROM   master_data
      WHERE  main_fuel    = 'WI'
        AND  extract_date = (SELECT MAX(extract_date)
                             FROM master_data))
  AND kpi_cd IN (
    -- Official Input KPIs (23)
    'PBES0020','PCAP000A','PCAP0010','PCAP001A',
    'PGEN0010','PGEN001S','PGEN0100','PGEN0101',
    'PGEN130A','PGEN0140','PGEN014G','PGEN014E','PGEN014M',
    'PGEN0147','PGEN0150','PGEN0180','PGEN0190',
    'PGEN019G','PGEN019E','PGEN019M',
    'PGEN0200','PGEN0250','PWDX0001',
    -- Official Calculated KPIs (24)
    'PCAP000F','PGEN0090','PGEN0096','PGEN0097',
    'PGEN0110','PGEN0148','PGENR090',
    'PKPI0011','PKPI0012','PKPI0146','PKPI0147','PKPI0148',
    'PKPI0150','PKPI0180','PKPI0190','PKPI0225',
    'PKPI0251','PKPI0253',
    'PKPIR094','PKPIR105','PKPIR110','PKPIR140',
    'PKPIR14G','PKPIR14E','PKPIR14M',
    -- Additional useful codes (3)
    'PGEN0094',  -- Net available electrical energy
    'PGEN0130',  -- Forced unplanned unavailability
    'PGEN0120'   -- Maintenance postponable unavailability
  )
;
