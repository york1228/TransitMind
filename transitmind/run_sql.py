"""Read-only GTFS queries and bounded foreign-key joins."""

import json
from typing import Any

import psycopg
import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.errors import SqlglotError

from .models import ToolResult
from .postgres_check import (
    CHECK_SQL, EXPECTED_DATABASE, EXPECTED_QUERY_USER, READ_ONLY_OPTIONS, DatabaseSettings,
)


MAX_ROWS = 100
# 模型始终只看前 12 行；运行记录和未来页面保留最多 100 行。
MODEL_PREVIEW_ROWS = 12
MAX_SQL_LENGTH = 8000
ALLOWED_TABLES = {"agency", "routes", "stops", "calendar", "calendar_dates", "trips", "stop_times"}
SQL_READ_ONLY_OPTIONS = READ_ONLY_OPTIONS + " -c search_path=pg_catalog,public -c lock_timeout=5000"

# 本课开放七张表的单表读取，以及一条已声明关系上的两表 JOIN。
# 停站记录通过 trip_id 连到班次，通过 stop_id 连到站点；日历表仍单独查询。
# 用解析后的节点限定能力，不靠危险关键词黑名单；不是完整生产 SQL 安全策略。
ALLOWED_NODES = (
    exp.Select, exp.From, exp.Table, exp.TableAlias, exp.Identifier,
    exp.Column, exp.Star, exp.Alias, exp.Literal, exp.Null, exp.Boolean,
    exp.Where, exp.Order, exp.Ordered, exp.Group, exp.Having, exp.Limit, exp.Distinct, exp.Join,
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
    exp.And, exp.Or, exp.Not, exp.Is, exp.In, exp.Between, exp.Like, exp.ILike, exp.Paren,
    exp.Count, exp.Lower, exp.Upper,
)

DATABASE_ERRORS = {
    "42703": ("unknown_column", "Column not found. Call describe_table and correct the SQL."),
    "42P01": ("unknown_table", "Table not found. Only the configured public GTFS tables are allowed."),
    "42501": ("permission_denied", "Query permission denied. Do not change accounts or retry this operation."),
    "57014": ("query_timeout", "The query exceeded its time limit. Simplify the SQL before retrying."),
    "42601": ("sql_syntax_error", "PostgreSQL rejected the SQL syntax. Correct the PostgreSQL query."),
}


def prepare_sql(sql: str) -> str:
    # SQL 字符串先变为语法树；成功解析不等于 SQL 一定正确，还要由数据库执行。
    try:
        statements = [item for item in sqlglot.parse(sql, read="postgres") if item is not None]
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise ValueError("Provide exactly one SELECT statement, without writes or commands.")
        statement = statements[0]
        if len(list(statement.find_all(exp.Select))) != 1:
            raise ValueError("Subqueries are not enabled in this lesson.")
        for node in statement.walk():
            if type(node) not in ALLOWED_NODES:
                raise ValueError(f"This lesson does not support {node.key}. Use a simple SELECT on the configured GTFS tables.")

        tables = list(statement.find_all(exp.Table))
        if not 1 <= len(tables) <= 2 or any(
            table.name not in ALLOWED_TABLES or table.db not in {"", "public"} or table.catalog for table in tables
        ):
            raise ValueError("Only the configured public GTFS tables are enabled in this lesson.")
        joins = statement.args.get("joins") or []
        if len(tables) == 2:
            aliases = {table.alias_or_name: table.name for table in tables}
            table_pair = frozenset(table.name for table in tables)
            join_keys = {
                frozenset(("agency", "routes")): "agency_id",
                frozenset(("routes", "trips")): "route_id",
                frozenset(("trips", "stop_times")): "trip_id",
                frozenset(("stops", "stop_times")): "stop_id",
            }
            if len(aliases) != 2 or table_pair not in join_keys or len(joins) != 1:
                raise ValueError("Use one declared agency/routes, routes/trips, trips/stop_times or stops/stop_times join; no self joins.")
            join = joins[0]
            on = join.args.get("on")
            if (join.side not in {"", "LEFT"} or join.kind not in {"", "INNER", "OUTER"}
                    or not isinstance(on, exp.EQ)):
                raise ValueError("Use INNER/LEFT JOIN with the declared foreign-key columns.")
            keys = (on.this, on.expression)
            expected_key = join_keys[table_pair]
            if (any(not isinstance(key, exp.Column) or key.name != expected_key for key in keys)
                    or {aliases.get(key.table) for key in keys} != table_pair):
                raise ValueError(f"JOIN must match the two qualified {expected_key} columns; filters belong in WHERE.")
        elif joins:
            raise ValueError("A join must use one declared relationship; standalone GTFS queries are allowed.")
        # 固定 public，避免同名表或临时表改变查询目标。
        for table in tables:
            table.set("db", exp.to_identifier("public"))

        # 保留用户更小的 LIMIT；多取一行，仅用于判断结果是否被截断。
        row_limit = MAX_ROWS + 1
        limit = statement.args.get("limit")
        if limit is not None:
            value = limit.expression
            if not isinstance(value, exp.Literal) or value.is_string or not value.this.isdecimal():
                raise ValueError("LIMIT must be a nonnegative integer.")
            row_limit = min(int(value.this), row_limit)
        statement = statement.limit(row_limit)
        return statement.sql(dialect="postgres", comments=False, unsupported_level=ErrorLevel.RAISE)
    except SqlglotError:
        raise ValueError("Cannot parse this PostgreSQL SQL. Use a simple SELECT on the configured GTFS tables.") from None


def run_sql(settings: DatabaseSettings, arguments: dict[str, Any]) -> ToolResult:
    if set(arguments) != {"sql"} or not isinstance(arguments.get("sql"), str):
        return ToolResult(False, "Provide exactly one string argument named sql.", "invalid_arguments")
    sql = arguments["sql"].strip()
    if not sql or len(sql) > MAX_SQL_LENGTH:
        return ToolResult(False, "SQL must contain 1 to 8000 characters.", "invalid_arguments")
    metadata: dict[str, Any] = {
        "tool_name": "run_sql", "source": "postgresql",
        "database": EXPECTED_DATABASE, "schema": "public",
        "read_only": True, "original_sql": sql,
    }
    try:
        executed_sql = prepare_sql(sql)
    except ValueError as error:
        return ToolResult(False, str(error), "sql_rejected", metadata=metadata)
    metadata["prepared_sql"] = executed_sql

    if (settings.host not in {"localhost", "127.0.0.1", "::1"}
            or settings.database != EXPECTED_DATABASE or settings.user != EXPECTED_QUERY_USER):
        return ToolResult(False, "Use the local transitmind database and transitmind_reader account.", "invalid_database", metadata=metadata)
    try:
        # 模型只提供 SQL，不接触凭据。每次连接开启只读和 5 秒超时，用完就关闭。
        with psycopg.connect(
            host=settings.host, port=settings.port, dbname=settings.database, user=settings.user,
            password=settings.password, connect_timeout=5, autocommit=True,
            options=SQL_READ_ONLY_OPTIONS, application_name="transitmind_run_sql",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(CHECK_SQL)
                if cursor.fetchone() != (EXPECTED_DATABASE, EXPECTED_QUERY_USER, "on"):
                    return ToolResult(False, "The actual database, account, or read-only state is incorrect.", "invalid_connection", metadata=metadata)
                # 执行的是经过解析和行数限制的 SQL；没有把模型输出直接当成可信代码。
                # 到这里才记录 executed_sql，表示确实尝试执行；结果由 success 标识。
                metadata["executed_sql"] = executed_sql
                cursor.execute(executed_sql)
                columns = [column.name for column in cursor.description]
                rows = cursor.fetchmany(MAX_ROWS + 1)
    except psycopg.Error as error:
        # 只返回可用于下一轮修正的错误类别，不泄露驱动原始连接诊断，不自动重试。
        code, message = DATABASE_ERRORS.get(
            error.sqlstate, ("sql_execution_error", "PostgreSQL query failed. Check the query and local connection."),
        )
        return ToolResult(False, message, code, metadata=metadata)

    truncated = len(rows) > MAX_ROWS
    # 日期、Decimal 等转换成 JSON 可保存的值；第 101 行只用来判断截断。
    result_rows = json.loads(json.dumps(rows[:MAX_ROWS], ensure_ascii=False, default=str))
    preview_rows = result_rows[:MODEL_PREVIEW_ROWS]
    preview_truncated = len(result_rows) > len(preview_rows)
    metadata.update({
        "columns": columns, "rows": result_rows, "row_count": len(result_rows),
        "row_limit": MAX_ROWS, "truncated": truncated,
        "preview_row_count": len(preview_rows), "preview_truncated": preview_truncated,
    })
    # 这份短预览交给模型；完整的保留结果通过 metadata 交给 trace 和页面。
    content = {
        "source": "postgresql", "database": EXPECTED_DATABASE, "schema": "public",
        "read_only": True, "sql": executed_sql, "columns": columns,
        "rows": preview_rows, "row_count": len(result_rows),
        "displayed_row_count": len(preview_rows), "row_limit": MAX_ROWS,
        "preview_truncated": preview_truncated, "truncated": truncated,
    }
    return ToolResult(True, json.dumps(content, ensure_ascii=False), metadata=metadata)
