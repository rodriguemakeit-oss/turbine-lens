from .databricks_tools_core.sql import execute_sql
from . import config
import os

catalog = os.environ.get("TURBINE_LENS_TRACES_CATALOG", "")
schema = os.environ.get("TURBINE_LENS_TRACES_SCHEMA", "")
prefix = os.environ.get("TURBINE_LENS_OTEL_TABLE_PREFIX", "")




query = [f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}",
        f"""CREATE TABLE if not exists {catalog}.{schema}.{prefix}_metrics  (
  record_id STRING,
  time TIMESTAMP,
  date DATE,
  service_name STRING,
  start_time_unix_nano LONG,
  time_unix_nano LONG,
  name STRING,
  description STRING,
  unit STRING,
  metric_type STRING,
  gauge STRUCT<
    value: DOUBLE,
    exemplars: ARRAY<STRUCT<
      time_unix_nano: LONG,
      value: DOUBLE,
      span_id: STRING,
      trace_id: STRING,
      filtered_attributes: VARIANT
    >>,
    attributes: VARIANT,
    flags: INT
  >,
  sum STRUCT<
    value: DOUBLE,
    exemplars: ARRAY<STRUCT<
      time_unix_nano: LONG,
      value: DOUBLE,
      span_id: STRING,
      trace_id: STRING,
      filtered_attributes: VARIANT
    >>,
    attributes: VARIANT,
    flags: INT,
    aggregation_temporality: STRING,
    is_monotonic: BOOLEAN
  >,
  histogram STRUCT<
    count: LONG,
    sum: DOUBLE,
    bucket_counts: ARRAY<LONG>,
    explicit_bounds: ARRAY<DOUBLE>,
    exemplars: ARRAY<STRUCT<
      time_unix_nano: LONG,
      value: DOUBLE,
      span_id: STRING,
      trace_id: STRING,
      filtered_attributes: VARIANT
    >>,
    attributes: VARIANT,
    flags: INT,
    min: DOUBLE,
    max: DOUBLE,
    aggregation_temporality: STRING
  >,
  exponential_histogram STRUCT<
    attributes: VARIANT,
    count: LONG,
    sum: DOUBLE,
    scale: INT,
    zero_count: LONG,
    positive_bucket: STRUCT<
      offset: INT,
      bucket_counts: ARRAY<LONG>
    >,
    negative_bucket: STRUCT<
      offset: INT,
      bucket_counts: ARRAY<LONG>
    >,
    flags: INT,
    exemplars: ARRAY<STRUCT<
      time_unix_nano: LONG,
      value: DOUBLE,
      span_id: STRING,
      trace_id: STRING,
      filtered_attributes: VARIANT
    >>,
    min: DOUBLE,
    max: DOUBLE,
    zero_threshold: DOUBLE,
    aggregation_temporality: STRING
  >,
  summary STRUCT<
    count: LONG,
    sum: DOUBLE,
    quantile_values: ARRAY<STRUCT<
      quantile: DOUBLE,
      value: DOUBLE
    >>,
    attributes: VARIANT,
    flags: INT
  >,
  metadata VARIANT,
  resource STRUCT<
    attributes: VARIANT,
    dropped_attributes_count: INT
  >,
  resource_schema_url STRING,
  instrumentation_scope STRUCT<
    name: STRING,
    version: STRING,
    attributes: VARIANT,
    dropped_attributes_count: INT
  >,
  metric_schema_url STRING
) USING DELTA
CLUSTER BY (time, service_name)
TBLPROPERTIES (
  'otel.schemaVersion' = 'v2',
  'delta.checkpointPolicy' = 'classic',
  'delta.enableVariantShredding' = 'true', -- optional
  'delta.feature.variantShredding' = 'supported', -- optional
  'delta.feature.variantType' = 'supported' -- optional
)""",

f"""CREATE TABLE if not exists {catalog}.{schema}.{prefix}_logs (
  record_id STRING,
  time TIMESTAMP,
  date DATE,
  service_name STRING,
  event_name STRING,
  trace_id STRING,
  span_id STRING,
  time_unix_nano LONG,
  observed_time_unix_nano LONG,
  severity_number STRING,
  severity_text STRING,
  body VARIANT,
  attributes VARIANT,
  dropped_attributes_count INT,
  flags INT,
  resource STRUCT<
    attributes: VARIANT,
    dropped_attributes_count: INT
  >,
  resource_schema_url STRING,
  instrumentation_scope STRUCT<
    name: STRING,
    version: STRING,
    attributes: VARIANT,
    dropped_attributes_count: INT
  >,
  log_schema_url STRING
) USING DELTA
CLUSTER BY (time, service_name)
TBLPROPERTIES (
  'otel.schemaVersion' = 'v2',
  'delta.checkpointPolicy' = 'classic',
  'delta.enableVariantShredding' = 'true', -- optional
  'delta.feature.variantShredding' = 'supported', -- optional
  'delta.feature.variantType' = 'supported' -- optional
)""",
 
f"""CREATE TABLE if not exists {catalog}.{schema}.{prefix}_spans (
  record_id STRING,
  time TIMESTAMP,
  date DATE,
  service_name STRING,
  trace_id STRING,
  span_id STRING,
  trace_state STRING,
  parent_span_id STRING,
  flags INT,
  name STRING,
  kind STRING,
  start_time_unix_nano LONG,
  end_time_unix_nano LONG,
  attributes VARIANT,
  dropped_attributes_count INT,
  events ARRAY<STRUCT<
    time_unix_nano: LONG,
    name: STRING,
    attributes: VARIANT,
    dropped_attributes_count: INT
  >>,
  dropped_events_count INT,
  links ARRAY<STRUCT<
    trace_id: STRING,
    span_id: STRING,
    trace_state: STRING,
    attributes: VARIANT,
    dropped_attributes_count: INT,
    flags: INT
  >>,
  dropped_links_count INT,
  status STRUCT<
    message: STRING,
    code: STRING
  >,
  resource STRUCT<
    attributes: VARIANT,
    dropped_attributes_count: INT
  >,
  resource_schema_url STRING,
  instrumentation_scope STRUCT<
    name: STRING,
    version: STRING,
    attributes: VARIANT,
    dropped_attributes_count: INT
  >,
  span_schema_url STRING
) USING DELTA
CLUSTER BY (time, service_name, trace_id)
TBLPROPERTIES (
  'otel.schemaVersion' = 'v2',
  'delta.checkpointPolicy' = 'classic',
  'delta.enableVariantShredding' = 'true', -- optional
  'delta.feature.variantShredding' = 'supported', -- optional
  'delta.feature.variantType' = 'supported' -- optional
)"""]


warehouse_id = config.SQL_WAREHOUSE_ID

def bootstrap():
    """
    Create the OpenTelemetry tables in the Databricks SQL warehouse if they do not exist.
    """
    for q in query:
        execute_sql(q, warehouse_id=warehouse_id)

    # set environment variables for the OpenTelemetry tables
    os.environ["TURBINE_LENS_OTEL_TABLE_PREFIX"] = f"{catalog}.{schema}.{prefix}"
