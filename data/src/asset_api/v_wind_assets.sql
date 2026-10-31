-- The wind asset dimension the `fleet_reporting` Genie space joins KPIs against, on
-- perform_unit_id. Sibling datasets referenced by BARE NAME so the pipeline places
-- them per target (see v_wind_kpis.sql).
--
-- WHY THE ROW_NUMBER MATTERS, and why this pipeline must NOT use CDC.
-- `master_data` holds MORE THAN ONE ROW PER UNIT inside a single extract, and this view
-- picks a specific one: highest-priority operational status first, then largest
-- capacity. A streaming table with `AUTO CDC ... STORED AS SCD TYPE 1` keyed on
-- perform_unit_id would instead keep whichever row arrived last — the same row count,
-- different rows, with nothing to indicate the answers had changed. Plain append plus
-- this ranking is deliberate, not a leftover.
--
-- The status-code meanings below exist nowhere else: Databricks strips `--` comments
-- from a stored view definition, so `information_schema.views` would return the CASE
-- expression with all 21 codes unexplained.
-- Declared as a plain VIEW, matching the original: computed on access, so it always
-- reflects the latest extract with no refresh to schedule. In a pipeline this publishes
-- a persistent view to Unity Catalog.
--
-- Worth knowing, not silently changed: `CREATE OR REFRESH MATERIALIZED VIEW` would
-- store the result instead, making Genie's queries far cheaper — v_wind_kpis reduces
-- ~775M rows to ~5.1M, and every question currently re-runs that reduction, including
-- two MAX() subqueries over the full table. The cost is staleness between refreshes.
-- That is a real trade and belongs to whoever owns the data, so the view stays a view.
CREATE VIEW v_wind_assets AS
WITH ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY perform_unit_id
      ORDER BY
        CASE unit_status_code
          WHEN 'EXT' THEN 1   -- Operating mode (active)
          WHEN 'AUG' THEN 2   -- Partial operation
          WHEN 'AUC' THEN 3   -- In execution / construction
          WHEN 'OPE' THEN 4   -- Operated
          WHEN 'EXG' THEN 5   -- Operating mode regroup
          WHEN 'EXS' THEN 6   -- Operating mode seasonal
          WHEN 'RAD' THEN 7   -- Running / adaptation
          WHEN 'DVO' THEN 8   -- DRR/VPP only
          WHEN 'MBO' THEN 9   -- Mothballed
          WHEN 'MBS' THEN 10  -- Mothballed seasonal
          WHEN 'MBP' THEN 11  -- Mothballed partial
          WHEN 'SPR' THEN 12  -- Secured project
          WHEN 'SPB' THEN 13  -- SPR with budget
          WHEN 'PRO' THEN 14  -- Project
          WHEN 'FDE' THEN 15  -- Final development
          WHEN 'DIS' THEN 16  -- Disinvestment
          WHEN 'EOO' THEN 17  -- End of operated
          WHEN 'EOC' THEN 18  -- End of contract
          WHEN 'CLS' THEN 19  -- Closed
          WHEN 'SLD' THEN 20  -- Sold
          WHEN 'ABO' THEN 21  -- Abandoned
          ELSE 99
        END,
        net_maximum_electrical_ac_capacity_mw DESC NULLS LAST
    ) AS rn
  FROM master_data
  WHERE main_fuel    = 'WI'
    AND extract_date = (SELECT MAX(extract_date) FROM master_data)
)
SELECT
  perform_unit_id, perform_unit_name, plant_id, plant_name,
  main_fuel_family, main_fuel_family_label, main_fuel, main_fuel_label,
  technology, technology_label, main_technology, main_technology_label,
  technology_profile_label, unit_status_code, unit_status_label,
  is_deleted, is_grc_unit, grc_group_name, global_business_line,
  global_business_unit_label, bus, bus_label, unit_metier, fleet_category,
  segment_label, sub_segment, activity, region_continentale, region,
  country_code, country_label, latitude_deg, longitude_deg,
  cod, cod_business_plan, ntp_date, commissioning_completion_date,
  acquisition_date, economic_end_date, strategic_end_date,
  strategic_end_date_corporate, commercial_out_of_service_date,
  net_maximum_electrical_ac_capacity_mw, gross_maximum_electrical_ac_capacity,
  net_grid_limited_electrical_ac_in_operation, wt__number_of_windturbines,
  unit_manufacturer, consomidation_method_label, consolidation_method_percent,
  consolidation_rate, consolidation_rate_type_label, net_ownership,
  project_currency, revenue_type_classification,
  revenue_type_contracted_percent, revenue_type_merchant_percent,
  revenue_type_regulated_percent,
  `revenue_type_contracted_fit/fip`   AS revenue_type_contracted_fit_fip,
  asset_grow, ancillary_services, grid_frequency_support, voltage_support,
  capacity_reserve, black_start, wholesale_market,
  energy_shifting__intraday, energy_shifting__dayahead, arbitrage,
  imbalance, congestion_management, network_congestion_relief,
  infrastructure_investment_deferral, res_integration,
  `capacity/profile_firming`          AS capacity_profile_firming,
  `ramp_rate_control/power_smoothing` AS ramp_rate_control_power_smoothing,
  reduce_point_of_connection_curtailment_losses,
  earth_perimeter, earth_environmental_permit_owned,
  earth_scope_percent, earth_scope_label, project_name,
  legal_entity_label, number_of_component, main_fuel_factbook,
  summer_time, extract_date
FROM ranked
WHERE rn = 1
;
