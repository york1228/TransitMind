"""Lesson 3.3: describe real PostgreSQL tables using read-only metadata queries."""

import json
from functools import partial
from typing import Any

import psycopg

from .models import ToolDefinition, ToolResult
from .postgres_check import CHECK_SQL, EXPECTED_DATABASE, READ_ONLY_OPTIONS, DatabaseSettings
from .tools import Tool, ToolRegistry


# 查询数据库保存的表结构，不读取 agency 的业务记录；表名作为参数，不拼进 SQL。
COLUMNS_SQL = """
SELECT c.column_name, c.data_type, c.is_nullable
FROM information_schema.columns AS c
JOIN information_schema.tables AS t
  ON t.table_catalog = c.table_catalog
 AND t.table_schema = c.table_schema
 AND t.table_name = c.table_name
WHERE c.table_schema = %s AND c.table_name = %s
  AND t.table_type = 'BASE TABLE'
ORDER BY c.ordinal_position
"""

# 只有 SELECT 权限时，table_constraints 可能不显示主键，改读 PostgreSQL 系统目录。
# conkey 保存主键字段编号；按编号在主键中的位置排序，保留联合主键的字段顺序。
PRIMARY_KEY_SQL = """
SELECT a.attname
FROM pg_catalog.pg_constraint AS con
JOIN pg_catalog.pg_class AS rel ON rel.oid = con.conrelid
JOIN pg_catalog.pg_namespace AS ns ON ns.oid = rel.relnamespace
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, position)
JOIN pg_catalog.pg_attribute AS a
  ON a.attrelid = rel.oid AND a.attnum = key_column.attnum
WHERE ns.nspname = %s AND rel.relname = %s
  AND con.contype = 'p'
ORDER BY key_column.position
"""

# 从真实约束读取连接关系，字段位置同时保留，联合外键也不会丢失对应顺序。
FOREIGN_KEYS_SQL = """
SELECT con.conname, child_col.attname, parent_ns.nspname,
       parent_rel.relname, parent_col.attname, key_column.position
FROM pg_catalog.pg_constraint AS con
JOIN pg_catalog.pg_class AS rel ON rel.oid = con.conrelid
JOIN pg_catalog.pg_namespace AS ns ON ns.oid = rel.relnamespace
JOIN pg_catalog.pg_class AS parent_rel ON parent_rel.oid = con.confrelid
JOIN pg_catalog.pg_namespace AS parent_ns ON parent_ns.oid = parent_rel.relnamespace
CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY
  AS key_column(child_attnum, parent_attnum, position)
JOIN pg_catalog.pg_attribute AS child_col
  ON child_col.attrelid = rel.oid AND child_col.attnum = key_column.child_attnum
JOIN pg_catalog.pg_attribute AS parent_col
  ON parent_col.attrelid = parent_rel.oid AND parent_col.attnum = key_column.parent_attnum
WHERE ns.nspname = %s AND rel.relname = %s AND con.contype = 'f'
ORDER BY con.conname, key_column.position
"""


def describe_table(settings: DatabaseSettings, arguments: dict[str, Any]) -> ToolResult:
    # 模型只提供一个表名；数据库地址、账号等仍由应用配置决定。
    if set(arguments) != {"table"} or not isinstance(arguments.get("table"), str):
        return ToolResult(False, "Provide exactly one string argument named table.", "invalid_arguments")
    table = arguments["table"]
    if not table.strip():
        return ToolResult(False, "The table name must not be empty.", "invalid_arguments")
    if settings.host not in {"localhost", "127.0.0.1", "::1"} or settings.database != EXPECTED_DATABASE:
        return ToolResult(False, "This lesson only reads the local transitmind database.", "invalid_database")

    try:
        # 复用连接检查的只读选项和超时。with 结束后关闭连接，不保留后台连接。
        with psycopg.connect(
            host=settings.host, port=settings.port, dbname=settings.database, user=settings.user,
            password=settings.password, connect_timeout=5, autocommit=True,
            options=READ_ONLY_OPTIONS, application_name="transitmind_schema_tool",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(CHECK_SQL)
                check = cursor.fetchone()
                if check is None or len(check) != 3 or check[0] != EXPECTED_DATABASE:
                    return ToolResult(False, "The connected database is not transitmind.", "invalid_database")
                if check[1] != settings.user:
                    return ToolResult(False, "The connected account does not match the configuration.", "invalid_user")
                if check[2] != "on":
                    return ToolResult(False, "The metadata connection must be read-only.", "read_only_required")

                # execute 发送固定 SQL；fetchall 取回多行，每行描述一个字段。
                parameters = ("public", table)# 查询 public 这个命名空间中的 agency 表
                cursor.execute(COLUMNS_SQL, parameters)
                columns = cursor.fetchall()
                if not columns:
                    return ToolResult(
                        False, "No visible base table with that name was found in public.", "unknown_table"
                    )
                cursor.execute(PRIMARY_KEY_SQL, parameters)
                primary_key = [row[0] for row in cursor.fetchall()]
                cursor.execute(FOREIGN_KEYS_SQL, parameters)
                foreign_keys = cursor.fetchall()
    except psycopg.Error:
        # 不把驱动原始错误送给模型或终端，避免泄露连接信息；本工具不自动重试。
        return ToolResult(
            False, "PostgreSQL metadata query failed. Check the local connection and permissions.",
            "schema_query_error",
        )

    # 保留 columns 字段供现有脚本模型读取，同时补充类型、可空性和主键。
    content = {
        "source": "postgresql",
        "database": EXPECTED_DATABASE,
        "schema": "public",
        "table": table,
        "read_only": True,
        "columns": [row[0] for row in columns],
        "column_details": [
            {"name": name, "data_type": data_type, "nullable": nullable == "YES"}
            for name, data_type, nullable in columns
        ],
        "primary_key": primary_key,
        "foreign_keys": [
            {"constraint": constraint, "column": column, "position": position,
             "referenced_schema": schema, "referenced_table": target, "referenced_column": target_column}
            for constraint, column, schema, target, target_column, position in foreign_keys
        ],
    }
    return ToolResult(True, json.dumps(content, ensure_ascii=False))


def build_postgres_tools(
    settings: DatabaseSettings, *, include_sql: bool = False, include_schema_memory: bool = False,
    schema_retrieval_mode: str = "keyword",
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="describe_table",
                description=(
                    "Read real PostgreSQL column names, data types, nullability, primary keys and foreign keys "
                    "for a visible base table in public. Does not read business rows or execute generated SQL."
                ),
                parameters={
                    "type": "object",
                    "properties": {"table": {"type": "string", "minLength": 1}},
                    "required": ["table"],
                    "additionalProperties": False,
                },
            ),
            # partial 先固定 settings；注册表调用时只需再传入模型给出的 arguments。
            handler=partial(describe_table, settings),
        )
    )
    if include_schema_memory:
        from .schema_memory import build_schema_tool

        # 新工具负责找相关表；原 describe_table 仍可读取最新结构，run_sql 仍负责业务查询。
        registry.register(build_schema_tool(settings.database, schema_retrieval_mode))
    if include_sql:
        from .run_sql import run_sql

        registry.register(Tool(
            definition=ToolDefinition(
                name="run_sql",
                description=(
                    "Execute one read-only PostgreSQL SELECT on the configured public GTFS tables after retrieving schemas. "
                    "Supports simple WHERE, ORDER BY, GROUP BY, COUNT, LOWER and UPPER. "
                    "One INNER or LEFT JOIN is allowed between agency/routes on agency_id, routes/trips on route_id, "
                    "trips/stop_times on trip_id or stops/stop_times on stop_id with explicit ON; calendar tables are standalone. "
                    "No cross joins, subqueries, WITH, writes or arbitrary functions. Returns column names, "
                    "up to 12 preview rows, the retained row count (up to 100) and truncation flags; execution is limited to 5 seconds."
                ),
                parameters={
                    "type": "object",
                    "properties": {"sql": {"type": "string", "minLength": 1, "maxLength": 8000}},
                    "required": ["sql"], "additionalProperties": False,
                },
            ),
            handler=partial(run_sql, settings),
        ))
    return registry
