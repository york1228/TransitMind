"""Lesson 3.2: check the local project database without an LLM or data writes."""
# 负责让 Python 与 PostgreSQL 通信
import argparse
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from dotenv import load_dotenv


EXPECTED_DATABASE = "transitmind"
EXPECTED_QUERY_USER = "transitmind_reader"
CHECK_SQL = "SELECT current_database(), current_user, current_setting('transaction_read_only')"
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=5000"


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


@dataclass(frozen=True)
class ConnectionCheck:
    database_name: str
    user_name: str
    read_only: bool


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


def check_connection(settings: DatabaseSettings) -> ConnectionCheck:
    try:
        # 连接配置逐项传入驱动，不拼接含密码的 URL；with 结束时自动关闭连接。
        with psycopg.connect(
            host=settings.host,
            port=settings.port,
            dbname=settings.database,
            user=settings.user,
            password=settings.password,
            connect_timeout=5,
            autocommit=True,
            options=READ_ONLY_OPTIONS,
            application_name="transitmind_connection_check",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(CHECK_SQL)
                row = cursor.fetchone()
                # execute() 把 SQL 送进去执行；fetchone() 把一行结果取回来。
    except psycopg.Error:
        raise DatabaseCheckError(
            "PostgreSQL connection or check failed. Check the local server, database, user, and password."
        ) from None

    if row is None or len(row) != 3:
        raise DatabaseCheckError("PostgreSQL did not return the expected connection information.")
    if row[0] != EXPECTED_DATABASE:
        raise DatabaseCheckError("The connected database is not transitmind; the check was rejected.")
    if row[1] != settings.user:
        raise DatabaseCheckError("The connected account does not match the configured account.")
    if row[2] != "on":
        raise DatabaseCheckError("The connection is not in read-only mode; the check was rejected.")
    return ConnectionCheck(row[0], row[1], True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lesson 3.2: a read-only PostgreSQL connection check, without an LLM.")
    parser.add_argument("--query", action="store_true", help="Check the dedicated Agent query account.")
    args = parser.parse_args(argv)
    try:
        result = check_connection(load_database_settings(for_query=args.query))
    except DatabaseCheckError as error:
        parser.exit(2, f"Error: {error}\n")

    mode = "postgres_query_check" if args.query else "postgres_check"
    print(f"Mode: {mode} (real database; read-only connection check; no LLM)")
    print(f"Database: {result.database_name}")
    print(f"Account: {result.user_name}")
    print(f"Session read-only: {'on' if result.read_only else 'off'}")
    print("No tables were created and no GTFS data was imported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
