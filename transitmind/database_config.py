"""Shared PostgreSQL settings for imports, read-only queries and diagnostics."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 保留原公共导出；只读查询选项的唯一实现移至 SQL 策略模块。
from .sql_policy import READ_ONLY_OPTIONS


EXPECTED_DATABASE = "transitmind"
EXPECTED_QUERY_USER = "transitmind_reader"
CHECK_SQL = "SELECT current_database(), current_user, current_setting('transaction_read_only')"


class DatabaseCheckError(RuntimeError):
    """A diagnostic that does not expose passwords or raw database errors."""


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    database: str
    user: str
    # 调试时打印配置对象，也不显示密码。
    password: str = field(repr=False)


def load_database_settings(*, for_query: bool = False) -> DatabaseSettings:
    # 只加载 TransitMind 的配置，不向上搜索 QueryMind 等项目的 .env。
    try:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    except (OSError, ValueError):
        raise DatabaseCheckError("Cannot load the local .env file. Check its permissions and format.") from None
    host = os.environ.get("TRANSITMIND_DB_HOST", "localhost").strip()
    database = os.environ.get("TRANSITMIND_DB_NAME", EXPECTED_DATABASE).strip()
    # 地址和数据库共用，登录凭据分开：导入用原账号，Agent 查询用 reader。
    # 查询密码缺失时直接拒绝，不退回原来的管理账号。
    prefix = "TRANSITMIND_QUERY_DB" if for_query else "TRANSITMIND_DB"
    default_user = EXPECTED_QUERY_USER if for_query else "postgres"
    user = os.environ.get(f"{prefix}_USER", default_user).strip()
    password = os.environ.get(f"{prefix}_PASSWORD", "")
    try:
        port = int(os.environ.get("TRANSITMIND_DB_PORT", "5432"))
    except ValueError:
        raise DatabaseCheckError("TRANSITMIND_DB_PORT must be an integer from 1 to 65535.") from None

    if not 1 <= port <= 65535:
        raise DatabaseCheckError("TRANSITMIND_DB_PORT must be an integer from 1 to 65535.")
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise DatabaseCheckError("Lesson 3.2 connects to a local PostgreSQL server only.")
    if database != EXPECTED_DATABASE:
        raise DatabaseCheckError("Set TRANSITMIND_DB_NAME to transitmind, not an existing business database.")
    if not user:
        raise DatabaseCheckError(f"{prefix}_USER must not be empty.")
    if for_query and user != EXPECTED_QUERY_USER:
        # 第三课先限定为刚验证过的角色，避免误把管理账号用于模型查询。
        raise DatabaseCheckError("Set TRANSITMIND_QUERY_DB_USER to transitmind_reader for this lesson.")
    if not password:
        raise DatabaseCheckError(f"Set {prefix}_PASSWORD in the local .env file. Do not send it in chat.")
    return DatabaseSettings(host, port, database, user, password)
