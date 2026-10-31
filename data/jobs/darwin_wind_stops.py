# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# For batch mode
dbutils.widgets.text("env", "dev", label="Default env")
dbutils.widgets.text("start_date", "", label="Input Start Date")
dbutils.widgets.text("end_date", "", label="Input End Date")

dbutils.widgets.text("target_catalog", "", label="Target Catalog")
dbutils.widgets.text("target_schema", "", label="Target Schema")


env = dbutils.widgets.get("env")
start_date = dbutils.widgets.get("start_date")
end_date = dbutils.widgets.get("end_date")
target_catalog = dbutils.widgets.get("target_catalog")
target_schema = dbutils.widgets.get("target_schema")

target_table = f"{target_catalog}.{target_schema}.wind_stops"

# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE TABLE IF NOT EXISTS
# MAGIC ${target_catalog}.${target_schema}.wind_stops (
# MAGIC timestamp TIMESTAMP comment 'The end-of-interval timestamp of the tag (the 10:10:00.000 data point represents data from 10:00:00.000 to 10:10:00.000)',
# MAGIC farm_code STRING comment 'The farm entity code (farm_code)',
# MAGIC ent_code STRING comment 'The entity code',
# MAGIC is_stopped BOOLEAN comment 'Indicates whether a stop-loss event occurred during the recorded interval',
# MAGIC power_loss DOUBLE comment 'Amount of power lost during the recorded interval',
# MAGIC year string,
# MAGIC month string
# MAGIC ) USING DELTA
# MAGIC PARTITIONED BY (farm_code, year, month)
# MAGIC TBLPROPERTIES (
# MAGIC   delta.autoOptimize.optimizeWrite = true,
# MAGIC   delta.autoOptimize.autoCompact = true,
# MAGIC   delta.enableChangeDataFeed = true,
# MAGIC   telemetry.drp.ignore = true,
# MAGIC   telemetry.drp.recovery_mode = 'full'
# MAGIC )

# COMMAND ----------

# COMMAND ----------

# MAGIC %run ./merge_tools

# COMMAND ----------

import pyspark.sql.functions as F

# COMMAND ----------

from pyspark.sql import functions as F
import pyspark
from pyspark.sql.dataframe import DataFrame
from pyspark.sql.window import Window
from delta.tables import DeltaTable

# COMMAND ----------

from pyspark.sql import Row, DataFrame, Column
from typing import List, Union, Any


# COMMAND ----------

def wind_stops(data, source_table, target_table):
    spark = data.sparkSession
    
    columns = [c.name for c in spark.catalog.listColumns(source_table)]
    # Retrieve actual version of the cdc table.    
    if all(x in data.columns for x in ["_change_type", "_commit_version", "_commit_timestamp"]):
        data = get_dataframe_from_cdc(data, ["ent_code", "date", "farm_code"], ignore_deleted_rows=False)

    
    data_to_delete = data.filter(F.col("_change_type") == "delete").select(columns).withColumnRenamed("date", "timestamp")
    data_to_merge = data.filter(F.col("_change_type") != "delete").select(columns).withColumnRenamed("date", "timestamp")
    
    tap = spark.table("telemetry_turbine_lens.theoretical_active_power.wind_10min")
    # Filter the tap table
    tap= filter_table_by_scope(scope = data_to_merge, data=tap, table_name="telemetry_turbine_lens.theoretical_active_power.wind_10min")

    tap = tap.where(F.col("generation_method")=="NacelleTransferFunction").select("farm_code", "timestamp", "ent_code", "TheoreticalActivePower").withColumnRenamed("TheoreticalActivePower", "theoretical_active_power")

    
    joined = data_to_merge.join(tap, on=["ent_code", "timestamp","farm_code"])
    results = joined.fillna(subset=["is_faulted_cds", "is_stopped_slb"], value=0).withColumn("is_stopped", (F.col("is_faulted_cds")==1)| (F.col("is_stopped_slb")==1)).withColumn("power_loss", F.when(F.col("is_stopped"), F.greatest(F.col("theoretical_active_power")-F.col("active_power"), F.lit(0))).otherwise(F.lit(None)))
    
    merge_to_table(target_table, 
                   data_to_merge=results.select("timestamp", "farm_code", "ent_code", "is_stopped", "power_loss", "year", "month"), 
                   data_to_delete=data_to_delete,
                   primary_keys=["ent_code", "timestamp", "farm_code"])

# COMMAND ----------

def wind_stops_from_tap(tap, source_table, target_table):
    spark = tap.sparkSession

    tap = tap.where(F.col("generation_method")=="NacelleTransferFunction")

    # Retrieve actual version of the cdc table.    
    if all(x in tap.columns for x in ["_change_type", "_commit_version", "_commit_timestamp"]):
        tap = get_dataframe_from_cdc(tap, ["ent_code", "timestamp", "farm_code", "generation_method"]).select("farm_code", "timestamp", "ent_code", "TheoreticalActivePower").withColumnRenamed("TheoreticalActivePower", "theoretical_active_power")

    # Never delete from the tap table, only merge new data. The stop-loss table is the source of truth for deletions.
    data = spark.table(source_table)
    data = data.withColumnRenamed("date", "timestamp")
    # Filter the tap table
    data= filter_table_by_scope(scope = tap, data=data, table_name=source_table)

    joined = data.join(tap, on=["ent_code", "timestamp","farm_code"]).drop_duplicates()
    results = joined.fillna(subset=["is_faulted_cds", "is_stopped_slb"], value=0).withColumn("is_stopped", (F.col("is_faulted_cds")==1)| (F.col("is_stopped_slb")==1)).withColumn("power_loss", F.when(F.col("is_stopped"), F.greatest(F.col("theoretical_active_power")-F.col("active_power"), F.lit(0))).otherwise(F.lit(None)))
    merge_to_table(target_table, results.select("timestamp", "farm_code", "ent_code", "is_stopped", "power_loss", "year", "month"), 
                   data_to_delete=None,
                   primary_keys=["ent_code", "timestamp", "farm_code"])

# COMMAND ----------

batch = start_date or end_date
source_table = "turbine_lens_telemetry_data.stoplogbook.mismatch_wind_10min"
tap_table = "turbine_lens_telemetry_data.theoretical_active_power.wind_10min"
checkpoint_location = f"/Volumes/{target_catalog}/{target_schema}/checkpoints/wind_stops"
checkpoint_location_tap = f"/Volumes/{target_catalog}/{target_schema}/checkpoints/wind_stops_tap"

if batch:
    # load batch data
    filter_data = F.lit(True)
    if start_date:
        filter_data &= F.col("timestamp") >= F.lit(start_date)
    if end_date:
        filter_data &= F.col("timestamp") <= F.lit(end_date)    

    batch = spark.read.table(source_table).where(filter_data)
    wind_stops(batch, source_table=source_table, target_table=target_table)

else:
    dfstream = spark.readStream.format("delta").option("readChangeFeed", "true").table(source_table)
    dfstream.writeStream.format("delta").option(
        "checkpointLocation",checkpoint_location,
    ).foreachBatch(lambda batch, id: wind_stops(batch, source_table=source_table, target_table=target_table)).trigger(availableNow=True).outputMode(
        "update"
    ).start().awaitTermination() # waiting for the end before the next stream starts

    

# COMMAND ----------

if not batch:
    dfstream = spark.readStream.format("delta").option("readChangeFeed", "true").table(tap_table)
    dfstream.writeStream.format("delta").option(
        "checkpointLocation",checkpoint_location_tap,
    ).foreachBatch(lambda batch, id: wind_stops_from_tap(batch, source_table=source_table, target_table=target_table)).trigger(availableNow=True).outputMode(
        "update"
    ).start()