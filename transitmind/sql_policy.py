"""Explicit SQL query permissions and limits shared by execution and prompts."""


SQL_SCHEMA = "public"
ALLOWED_TABLES = {"agency", "routes", "stops", "calendar", "calendar_dates", "trips", "stop_times"}

# 这是人工明确授权的关系，不从数据库外键、检索结果或 AI 说明自动增加。
ALLOWED_JOINS = {
    ("agency", "routes"): "agency_id",
    ("routes", "trips"): "route_id",
    ("trips", "stop_times"): "trip_id",
    ("stops", "stop_times"): "stop_id",
}
# 校验时忽略两张表的书写顺序，和原执行器的 frozenset 匹配方式一致。
JOIN_KEYS = {frozenset(tables): column for tables, column in ALLOWED_JOINS.items()}
MAX_TABLES = 3
MAX_JOINS = MAX_TABLES - 1
# SQLGlot 用 side/kind 表示 JOIN；保留原来接受的组合，包括 LEFT OUTER 的写法。
ALLOWED_JOIN_SIDES = ("", "LEFT")
ALLOWED_JOIN_KINDS = ("", "INNER", "OUTER")
ALLOWED_FUNCTIONS = ("COUNT", "LOWER", "UPPER", "MIN", "MAX")

MAX_ROWS = 100
# 模型始终只看前 12 行；运行记录和未来页面保留最多 100 行。
MODEL_PREVIEW_ROWS = 12
ANSWER_SAMPLE_ROWS = 5
MIN_SQL_LENGTH = 1
MAX_SQL_LENGTH = 8000
QUERY_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 5000
CONNECT_TIMEOUT_SECONDS = 5

# 这些选项只用于只读查询和连接检查；导入器继续维护自己的写入选项与超时。
READ_ONLY_OPTIONS = f"-c default_transaction_read_only=on -c statement_timeout={QUERY_TIMEOUT_MS}"
SQL_READ_ONLY_OPTIONS = (
    READ_ONLY_OPTIONS
    + f" -c search_path=pg_catalog,{SQL_SCHEMA} -c lock_timeout={LOCK_TIMEOUT_MS}"
)


def join_type_description() -> str:
    return " or ".join(side or "INNER" for side in ALLOWED_JOIN_SIDES)


def sql_scope_description() -> str:
    # 同一份授权数据同时用于执行校验、工具说明和系统提示词。
    tables = ", ".join(f"{SQL_SCHEMA}.{table}" for table in sorted(ALLOWED_TABLES))
    joins = "; ".join(
        f"{left}.{column} = {right}.{column}"
        for (left, right), column in ALLOWED_JOINS.items()
    )
    joined_tables = {table for pair in ALLOWED_JOINS for table in pair}
    standalone_tables = sorted(ALLOWED_TABLES - joined_tables)
    standalone_description = (
        f" {', '.join(standalone_tables)} are currently queried as standalone tables."
        if standalone_tables else ""
    )
    return (
        f"The allowed tables are {tables}. "
        f"Use one read-only PostgreSQL SELECT with at most {MAX_TABLES} tables. "
        f"Supports simple WHERE, ORDER BY, GROUP BY, HAVING, DISTINCT and {', '.join(ALLOWED_FUNCTIONS)}. "
        f"Use at most {MAX_JOINS} {join_type_description()} JOINs along these declared relationships: {joins}. "
        "Each added table must join to an earlier table using exactly one equality between their qualified "
        "relationship columns in ON; put other filters in WHERE. "
        f"{standalone_description} "
        "No cross/self joins, WITH, subqueries, casts, other functions or writes. "
    )
