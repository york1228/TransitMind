"""Decide whether a failed SQL call can be corrected within the current run."""


# 输入是 run_sql 已有的错误码；这里只决定处理方向，SQL 仍由模型修改、原工具校验执行。
_RECOVERY_RULES = {
    "invalid_arguments": (
        "correct_sql", "按工具参数定义提供 SQL，修正参数后再尝试。",
    ),
    "sql_rejected": (
        "correct_sql", "根据校验诊断修正 SQL；遵守现有表、关联和语法限制，不修改执行策略。",
    ),
    "unknown_column": (
        "correct_sql", "先用 describe_table 确认真实字段，再修正 SQL；不要猜字段名。",
    ),
    "unknown_table": (
        "correct_sql", "用 Schema 工具确认当前允许的表，再修正表名；缺少所需表时说明限制。",
    ),
    "sql_syntax_error": (
        "correct_sql", "根据诊断修正 PostgreSQL 语法，再通过 run_sql 执行。",
    ),
    "query_timeout": (
        "correct_sql", "在保留问题原意的前提下简化查询；不原样重试，不提高超时或扩大返回范围。",
    ),
    "permission_denied": (
        "stop", "查询账号没有所需读取权限，本次运行停止；请由管理员检查授权。",
    ),
    "invalid_database": (
        "stop", "数据库或查询账号不符合项目配置，本次运行停止；请检查连接配置。",
    ),
    "invalid_connection": (
        "stop", "实际数据库、账号或只读状态不符合要求，本次运行停止；请检查连接配置。",
    ),
}


def decide_sql_recovery(error_code: str | None) -> dict[str, str]:
    # 未分类错误可能来自连接或工具本身，当前小节不自动重试，保留原诊断供排查。
    action, instruction = _RECOVERY_RULES.get(error_code, (
        "stop", "当前错误不属于已支持的 SQL 修正范围，本次运行停止；请检查工具诊断。",
    ))
    return {"action": action, "instruction": instruction}
