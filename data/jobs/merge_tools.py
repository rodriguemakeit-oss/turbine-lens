# Databricks notebook source
# COMMAND ----------
from pyspark.sql import functions as F
import pyspark
from pyspark.sql.dataframe import DataFrame
from pyspark.sql.window import Window
from delta.tables import DeltaTable

def get_partition_pruning_conditions(data: DataFrame, table_name: str, primary_keys, extra_condition = None) -> str:
    spark = data.sparkSession
    merge = []
    for col in primary_keys:
        condition_string = f"t.{col} <=> u.{col}"
        merge.append(condition_string)

    # Get the partition columns    
    columns = spark.catalog.listColumns(table_name)

    # 2. Filter out partition columns
    partition_columns = list(c.name for c in filter(lambda c: c.isPartition, list(columns)))    
    # Merging special case: partition pruning
    if partition_columns:
        partitions_pruning = []
        partitions = (
            data.select(partition_columns)
            .groupby(partition_columns[:-1])
            .agg(
                F.collect_set(partition_columns[-1]).alias(
                    partition_columns[-1]
                )
            )
            .collect()
        )
        for row in partitions:
            partition_pruning = []
            for p in partition_columns[:-1]:
                condition_string = f"t.{p} = '{row[p]}'"
                partition_pruning.append(condition_string)
            p = partition_columns[-1]
            condition_string = f"t.{p} IN ({str(sorted(row[p]))[1:-1]})"
            partition_pruning.append(condition_string)
            partitions_pruning.append("(" + " AND ".join(partition_pruning) + ")")

        if partitions_pruning:
            pruning = "(" + " OR ".join(partitions_pruning) + ")"
            # print(pruning)
            merge.append(pruning)

    if extra_condition:
        merge.append(extra_condition)

    merge_strings = " AND ".join(merge)
    return merge_strings


def merge_to_table(table_name: str, data_to_merge: pyspark.sql.DataFrame, 
                   data_to_delete: pyspark.sql.DataFrame,
                   primary_keys, ignore_cols = [], extra_condition = None) -> pyspark.sql.DataFrame:
    """Write the output_data into a table; using the merge method.
    The merge is perform as follow:
        - checking the output primary keys
        - ok on changing in the specific output_columns

    We also add the writetime for each row.
    Parameters
    ----------
    output_data: pyspark.sql.DataFrame. Can also be a streaming.
    The dataframe that should be merge in the deltatable.

    Returns
    -------
    A pyspark dataframe for the specific rows of the scoped data.
    """
    # need to establish a format for the merge conditions
    spark = data_to_merge.sparkSession


    # Data to delete
    if data_to_delete is not None:
        if not data_to_delete.isEmpty():
            delete_strings = get_partition_pruning_conditions(data_to_delete, table_name, primary_keys, 
                                                              extra_condition)
            df_target_table = _delete_row_from_table(table_name, data_to_delete, delete_strings)
    

    # Data to merge
    columns_to_compare = list(data_to_merge.columns)
    for col in ignore_cols:
        columns_to_compare.remove(col)

    condition = []
    for col in columns_to_compare:
        condition_string = f"NOT(t.{col} <=> u.{col})"
        condition.append(condition_string)

    string_conditions = " OR ".join(condition)
    merge_strings = get_partition_pruning_conditions(data_to_merge, table_name, primary_keys, extra_condition)
    
    df_target_table = _merge_table(table_name, data_to_merge, merge_strings, string_conditions)
    return df_target_table

def _delete_row_from_table(table_name, data_to_delete, merge_strings = None):
    """Delete the rows from the table based on the primary keys and the extra condition.
    Parameters
    ----------
    data_to_delete: pyspark.sql.DataFrame. Can also be a streaming.
    The dataframe that should be delete in the deltatable.

    Returns
    -------
    A pyspark dataframe for the specific rows of the scoped data.
    """
    
    # print(merge_strings)
    spark = data_to_delete.sparkSession
    df_target_table = DeltaTable.forName(spark, table_name)
    df_target_table.alias("t").merge(data_to_delete.alias("u"), merge_strings).whenMatchedDelete().execute()
    return df_target_table

def _merge_table(table_name, output_data, merge_conditions, update_conditions):
    """
    Internal method to proceed the merge

    Parameters
    ----------
    output_data: data to merge
    merge_strings : merge conditions
    string_conditions : update condition

    Returns
    -------

    """    
    # Loading table for merge
    spark = output_data.sparkSession
    df_target_table = DeltaTable.forName(spark, table_name)
    df_target_table.alias("t").merge(output_data.alias("u"), merge_conditions).whenMatchedUpdateAll(
        condition=update_conditions
    ).whenNotMatchedInsertAll().execute()
    return df_target_table


# COMMAND ----------

from pyspark.sql import Row, DataFrame, Column
from typing import List, Union, Any
def get_dataframe_from_cdc(df: DataFrame,
                               primary_keys: List[Union[str, Column]],
                               order_by: List[Union[str, Column]] = None,
                               ignore_deleted_rows: bool = True):

    if order_by is None:
        order_by = [F.col("_commit_version").desc(), F.col("_change_type").desc_nulls_last()]

    assert '_commit_version' in df.columns, "column not found '_commit_version'"
    assert '_change_type' in df.columns, "column not found '_change_type'"
    for pk in primary_keys:
        assert pk in df.columns, f"column not found '{pk}'"

    w = Window.partitionBy(*primary_keys).orderBy(*order_by)

    window_computed_change_type = Window.partitionBy(*primary_keys) \
        .orderBy(F.col("row_number").desc())

    df_filtered = df.filter(F.col('_change_type').isin(["insert", "update_postimage", "delete"])) \
        .withColumn("row_number", F.row_number().over(w)) \
        .withColumn("_computed_first_change_type", F.first("_change_type").over(window_computed_change_type)) \
        .withColumn("_computed_change_type", F.when(((F.col("_change_type") == F.lit("update_postimage"))
                                                    & (F.col("_computed_first_change_type") == F.lit("insert")))
                                                    , F.lit("insert")) \
                    .otherwise(F.col("_change_type"))) \
        .drop("_change_type", "_computed_first_change_type") \
        .withColumnRenamed("_computed_change_type", "_change_type") \
        .filter(F.col("row_number") == 1)

    if ignore_deleted_rows:
        df_filtered = df_filtered.where(F.col("_change_type") != F.lit("delete"))

    return df_filtered.drop("row_number").select(df.columns)

# COMMAND ----------

def filter_table_by_scope(scope: DataFrame, data: DataFrame, table_name):
    spark = scope.sparkSession    

    # 1.  Get table columns for given table
    columns = spark.catalog.listColumns(table_name)

    # 2. Filter out partition columns
    partition_columns = list(filter(lambda c: c.isPartition, columns))
    subset_conditions = {}
    for c in partition_columns:
        if c.name in scope.columns:
            subset_conditions[c.name] = [r[c.name] for r in scope.select(c.name).distinct().collect()]
    print(f"Filtering keys: {subset_conditions.keys()}")

    for k, v in subset_conditions.items():
        data = data.where(F.col(k).isin(v))
    

    return data

# COMMAND ----------
