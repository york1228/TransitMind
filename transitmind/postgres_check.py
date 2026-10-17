"""Lesson 3.2: check the local project database without an LLM or data writes."""
# 负责让 Python 与 PostgreSQL 通信
import argparse
from collections.abc import Sequence
from dataclasses import dataclass

import psycopg

# 兼容旧导入路径；公共配置只在 database_config 中实现一次。
from .database_config import (
    CHECK_SQL, EXPECTED_DATABASE, EXPECTED_QUERY_USER, READ_ONLY_OPTIONS,
    DatabaseCheckError, DatabaseSettings, load_database_settings,
)
from .sql_policy import CONNECT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ConnectionCheck:
    database_name: str
    user_name: str
    read_only: bool


def check_connection(settings: DatabaseSettings) -> ConnectionCheck:
    try:
        # 连接配置逐项传入驱动，不拼接含密码的 URL；with 结束时自动关闭连接。
        with psycopg.connect(
            host=settings.host,
            port=settings.port,
            dbname=settings.database,
            user=settings.user,
            password=settings.password,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
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
