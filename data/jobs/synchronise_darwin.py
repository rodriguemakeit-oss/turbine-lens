# Databricks notebook source
# For batch mode
dbutils.widgets.text("sp_turbine_lens_id","00000000-0000-0000-0000-000000000000", label="Service Principal ID")
dbutils.widgets.text("target_catalog", "turbine_lens_telemetry_data", label="Target Catalog")
dbutils.widgets.text("source_catalog", "telemetry_turbine_lens", label="Source Catalog")

# COMMAND ----------
sp_turbine_lens_id = dbutils.widgets.get("sp_turbine_lens_id")
telemetry_catalog = dbutils.widgets.get("source_catalog")
target_catalog = dbutils.widgets.get("target_catalog")

# COMMAND ----------

# MAGIC %run ./merge_tools

# COMMAND


from pyspark.sql import functions as F
import pyspark
from pyspark.sql.dataframe import DataFrame
from pyspark.sql.window import Window
from delta.tables import DeltaTable
# COMMAND ----------

spark.catalog.setCurrentCatalog(telemetry_catalog)
schemas = [s.name for s in spark.catalog.listDatabases() if s.name != "information_schema"]
# spark.catalog.listTables("theoretical_active_power")

# COMMAND ----------

for s in schemas:
    # print(f"{s.name} {[t.name for t in spark.catalog.listTables(s.name)]}")
    if s != "information_schema":
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {target_catalog}.{s}")
        spark.sql(f"GRANT SELECT ON SCHEMA {target_catalog}.{s} TO `{sp_turbine_lens_id}`")        
        spark.sql(f"GRANT CREATE_TABLE ON SCHEMA {target_catalog}.{s} TO `{sp_turbine_lens_id}`")
        spark.sql(f"GRANT USE_SCHEMA ON SCHEMA {target_catalog}.{s} TO `{sp_turbine_lens_id}`")

# COMMAND ----------

# Create technical env for checkpoints and temporary tables
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {target_catalog}.technical")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {target_catalog}.technical.checkpoints")
spark.sql(f"GRANT SELECT ON SCHEMA {target_catalog}.technical TO `{sp_turbine_lens_id}`")   
spark.sql(f"GRANT CREATE_TABLE ON SCHEMA {target_catalog}.technical TO `{sp_turbine_lens_id}`")


# COMMAND ----------

spark.sql(f"GRANT USE_SCHEMA ON SCHEMA {target_catalog}.technical TO `{sp_turbine_lens_id}`")
spark.sql(f"GRANT MANAGE ON VOLUME {target_catalog}.technical.checkpoints TO `{sp_turbine_lens_id}`")

# COMMAND ----------

# Create table for all schema
tables_to_stream = {}
for s in schemas:    
    tables = spark.catalog.listTables(s)
    for t in tables:                
        columns = spark.catalog.listColumns(".".join((telemetry_catalog,s,t.name)))        
        columns_str = []
        partitions_str = []
        for c in columns:
            col = f"{c.name} {str.upper(c.dataType)}"
            if c.description:
                col += f' comment "{c.description.replace("\"", "'")}"'
            columns_str.append(col)
            if c.isPartition:
                partitions_str.append(c.name)
        if partitions_str:
            partitions_str = f"PARTITIONED BY ({','.join(partitions_str)})"
        else:
            partitions_str = ""
        query = f"""CREATE TABLE IF NOT EXISTS {target_catalog}.{s}.{t.name} ({','.join(columns_str)}) USING DELTA
                  {partitions_str}
                  TBLPROPERTIES (delta.autoOptimize.autoCompact = true, delta.autoOptimize.optimizeWrite = true, delta.enableChangeDataFeed = true)"""
        spark.sql(query)
        tables_to_stream[f"{telemetry_catalog}.{s}.{t.name}"] = f"{target_catalog}.{s}.{t.name}"

# COMMAND ----------

tables_to_stream

# COMMAND ----------

import json
primary_keys = {}
cdc = {}
for t in tables_to_stream.keys():
    query = f"SHOW TBLPROPERTIES {t} (telemetry.keyColumns)"
    rows = spark.sql(query).collect()
    value = rows[0].value
    if "does not have property: telemetry.keyColumns" not in value:
        primary_keys[t] = json.loads(value)
    else:
        primary_keys[t] = None
# Create table for all schema

for t in tables_to_stream.keys():
    query = f"SHOW TBLPROPERTIES {t} (delta.enableChangeDataFeed)"
    rows = spark.sql(query).collect()
    value = rows[0].value
    if "does not have property: delta.enableChangeDataFeed" not in value:
        cdc[t] = True
    else:
        cdc[t] = False



# En attendant de trouver mieux
primary_keys["telemetry_turbine_lens.machine_learning.predictions_10min"] = ["timestamp", "farm_code", "ent_code", "method", "target", "specification"]
primary_keys["telemetry_turbine_lens.meteo.wind_era5_1h"] = ["timestamp", "farm_code", "direction"]
primary_keys["telemetry_turbine_lens.pic.hierarchy"] = ["farm_code", "substation_code"]
primary_keys["telemetry_turbine_lens.pic.wind_turbine"] = ["farm_code", "wind_turbine_code"]
primary_keys["telemetry_turbine_lens.referentials.alarm_model"] = ["alarm_model_id", "scada_code"]


# COMMAND ----------

def ingest_data(data, source_table):
    spark = data.sparkSession
    
    columns = [c.name for c in spark.catalog.listColumns(source_table)]
    
    # Primary keys are?
    p_key = primary_keys[source_table]
    target_table = tables_to_stream[source_table]

    # Retrieve actual version of the cdc table.    
    if all(x in data.columns for x in ["_change_type", "_commit_version", "_commit_timestamp"]):
        data = get_dataframe_from_cdc(data, p_key, ignore_deleted_rows=False)
    data_to_delete = data.filter(F.col("_change_type") == "delete").select(columns)
    data_to_merge = data.filter(F.col("_change_type") != "delete").select(columns)
    merge_to_table(target_table, data_to_merge=data_to_merge, data_to_delete=data_to_delete, primary_keys=p_key)

# COMMAND ----------


checkpoint_location = f"/Volumes/{target_catalog}/technical/checkpoints/"

# if batch:
#     # load batch data
#     filter_data = F.lit(True)
#     if start_date:
#         filter_data &= F.col("timestamp") >= F.lit(start_date)
#     if end_date:
#         filter_data &= F.col("timestamp") <= F.lit(end_date)    

#     batch = spark.read.table(source_table).where(filter_data)
#     ingest_data(batch, target_table)

# else:
for source_table in tables_to_stream.keys():
    
    
    if cdc[source_table]:
        dfstream = spark.readStream.format("delta").option("readChangeFeed", "true").table(source_table)

        dfstream.writeStream.format("delta").option(
            "checkpointLocation",checkpoint_location+source_table,
        ).foreachBatch(lambda batch, id: ingest_data(batch, source_table=source_table)).trigger(availableNow=True).outputMode(
            "update"
        ).start().awaitTermination() # waiting for the end before the next stream starts
    else:
        # overwrite the table
        spark.table(source_table).write.mode("overwrite").saveAsTable(tables_to_stream[source_table])

    