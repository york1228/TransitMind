"""Lesson 3.2: preview agency.txt, then explicitly import one small table."""

import argparse
import csv
import hashlib
import io
from collections.abc import Sequence
from dataclasses import asdict, astuple, dataclass
from pathlib import Path

import psycopg
from psycopg import cursor

from .database_config import DatabaseCheckError, DatabaseSettings, EXPECTED_DATABASE, load_database_settings


# 默认读取本项目中的数据副本；仍可用 --source 指定其他 GTFS 目录。
DEFAULT_SOURCE = Path(__file__).resolve().parent.parent / "data" / "raw" / "MBTA_GTFS"
# 本小步只处理当前 MBTA 文件的七个字段，不自动导入其他文件或忽略新字段。
AGENCY_COLUMNS = (
    "agency_id", "agency_name", "agency_url", "agency_timezone",
    "agency_lang", "agency_phone", "agency_fare_url",
)
REQUIRED_VALUES = ("agency_id", "agency_name", "agency_url", "agency_timezone")
IMPORT_OPTIONS = "-c statement_timeout=5000 -c lock_timeout=5000"
DATABASE_SQL = "SELECT current_database()"
EXISTS_SQL = "SELECT to_regclass('public.agency')"

# ID 和电话号码都是文本，不做算术；TEXT 能保留前导零、字母和连字符。
# PRIMARY KEY 保证机构 ID 唯一，NOT NULL 表示该列不接受数据库空值。
# 先创建表
CREATE_SQL = """
CREATE TABLE public.agency (
    agency_id TEXT PRIMARY KEY,
    agency_name TEXT NOT NULL,
    agency_url TEXT NOT NULL,
    agency_timezone TEXT NOT NULL,
    agency_lang TEXT,
    agency_phone TEXT,
    agency_fare_url TEXT
)
"""

# %(字段名)s 是驱动的参数占位符，不是 Python 字符串拼接。
# SQL 与每条记录的字典分别传给驱动，机构名称中的引号也按普通数据处理。
INSERT_SQL = """
INSERT INTO public.agency (
    agency_id, agency_name, agency_url, agency_timezone,
    agency_lang, agency_phone, agency_fare_url
) VALUES (
    %(agency_id)s, %(agency_name)s, %(agency_url)s, %(agency_timezone)s,
    %(agency_lang)s, %(agency_phone)s, %(agency_fare_url)s
)
"""
VERIFY_SQL = """
SELECT agency_id, agency_name, agency_url, agency_timezone,
       agency_lang, agency_phone, agency_fare_url
FROM public.agency
ORDER BY agency_id
"""


class AgencyImportError(ValueError):
    """An import diagnostic without credentials or raw database errors."""


@dataclass(frozen=True)
class Agency:
    # 一份 Agency 对象对应一行数据；可选字段空白时用 None，入库后为 NULL。
    agency_id: str
    agency_name: str
    agency_url: str
    agency_timezone: str
    agency_lang: str | None
    agency_phone: str | None
    agency_fare_url: str | None


@dataclass(frozen=True)
class AgencySnapshot:
    source_path: Path
    agencies: tuple[Agency, ...]
    sha256: str


@dataclass(frozen=True)
class ImportResult:
    database_name: str
    row_count: int


def read_agencies(source: Path) -> AgencySnapshot:
    # 读取文件，整理成 Python 中的数据对象。
    """Read and check the small agency file without connecting to PostgreSQL."""
    path = source.resolve() / "agency.txt"
    try:
        # 小文件只读一次：解析和哈希都使用同一份字节，不会各自读到不同版本。
        # 大型 stop_times 文件以后需要流式导入，不能照搬这里的整文件读取方式。
        raw = path.read_bytes()
        stream = io.StringIO(raw.decode("utf-8-sig"), newline="")
        # decode()：把字节转成文字；StringIO 让 CSV 解析器能够读取这段文字。
        reader = csv.DictReader(stream, strict=True)
        columns = reader.fieldnames or []
        if len(columns) != len(AGENCY_COLUMNS) or set(columns) != set(AGENCY_COLUMNS):
            raise AgencyImportError("agency.txt must contain exactly the seven columns supported in this lesson.")

        agencies: list[Agency] = []
        seen_ids: set[str] = set()
        for row in reader:
            # DictReader 用表头作为字典的键；多出来的值放在 None 键下，缺值则为 None。
            # 列数不齐时先拒绝，不能把格式错误当成合法的可选空值。
            if None in row or any(value is None for value in row.values()):
                raise AgencyImportError(f"agency.txt: record ending at line {reader.line_num} has a column mismatch.")
            for column in REQUIRED_VALUES:
                if not row[column].strip():
                    raise AgencyImportError(f"agency.txt: line {reader.line_num} has an empty required {column}.")
            if row["agency_id"] in seen_ids:
                raise AgencyImportError("agency.txt contains a duplicate agency_id.")
            seen_ids.add(row["agency_id"])

            # 保留原始字符串；只有可选字段的空字符串变为 None，不把空值改成 0。
            values = {column: row[column] or None for column in AGENCY_COLUMNS}
            # 它们是在做：把一行 CSV 字典变成 Agency 对象，再放进列表。
            agencies.append(Agency(**values))
            # 我们定义了 Agency 这个数据结构，每读取一条 row，就创建一个 Agency 对象，存放这一行各字段的值。
    except OSError:
        raise AgencyImportError("Cannot read agency.txt. Check the source directory and file permissions.") from None
    except (csv.Error, UnicodeError):
        raise AgencyImportError("agency.txt contains invalid UTF-8 or CSV structure.") from None

    if not agencies:
        raise AgencyImportError("agency.txt contains no agency records.")
    return AgencySnapshot(path, tuple(agencies), hashlib.sha256(raw).hexdigest())


def import_agencies(settings: DatabaseSettings, snapshot: AgencySnapshot) -> ImportResult:
    # 接收这些对象，连接数据库，建表并写入。
    """Create, insert, and verify one new table in a single transaction."""
    # 再次检查写入目标，防止直接调用这个函数时绕开配置加载器的检查。
    if settings.host not in {"localhost", "127.0.0.1", "::1"} or settings.database != EXPECTED_DATABASE:
        raise AgencyImportError("Import is allowed only into the local transitmind database.")
    if not snapshot.agencies:
        raise AgencyImportError("Cannot import an empty agency snapshot.")

    try:
        # 与只读检查不同，本命令需要建表和插入权限。
        # autocommit=False：这些 SQL 属于同一个事务，不会每执行一句就单独保存。
        # 正常离开连接的 with 块时提交；块内出错时回滚；两种情况都会关闭连接。
        with psycopg.connect(
            host=settings.host, port=settings.port, dbname=settings.database,
            user=settings.user, password=settings.password,
            connect_timeout=5, autocommit=False, options=IMPORT_OPTIONS,
            application_name="transitmind_agency_import",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(DATABASE_SQL)
                database_row = cursor.fetchone()
                if database_row != (EXPECTED_DATABASE,):
                    raise AgencyImportError("The connected database is not transitmind; no import was attempted.")

                cursor.execute(EXISTS_SQL)
                exists_row = cursor.fetchone()
                if exists_row != (None,):
                    raise AgencyImportError("public.agency already exists or could not be checked; nothing will be overwritten.")

                cursor.execute(CREATE_SQL)
                # asdict() 把 Agency 对象转成字段字典；executemany() 用同一条 SQL 写入多行。
                cursor.executemany(INSERT_SQL, [asdict(agency) for agency in snapshot.agencies])
                # 这里的 executemany() 是把所有 Agency 对象的字典一次性传给驱动，驱动会循环执行 INSERT_SQL，把每条记录写入数据库。
                # cursor.execute(CREATE_SQL) 执行一次：创建表，并定义七列。不是一次循环。
                # cursor.executemany(INSERT_SQL, ...)：把收集好的两条记录写进去，每条记录包含七个字段的值。

                # 写入后立即查回七列，逐值比较，而不是只凭 INSERT 没报错判断成功。
                cursor.execute(VERIFY_SQL)
                actual_rows = tuple(cursor.fetchall())
                # 数据库排序规则可能不同于 Python；比较整行内容，不要求排列顺序相同。
                expected_rows = {astuple(agency) for agency in snapshot.agencies}
                if len(actual_rows) != len(snapshot.agencies) or set(actual_rows) != expected_rows:
                    raise AgencyImportError("Imported rows differ from the source; the transaction will be rolled back.")
    except psycopg.errors.DuplicateTable:
        # 两个导入同时执行时，也可能在检查之后才发现重名；仍然不覆盖已有表。
        raise AgencyImportError("public.agency already exists; nothing will be overwritten.") from None
    except psycopg.Error:
        # 不打印包含连接细节的原始异常。提交时若断线，需查库确认，不能盲目重试。
        raise AgencyImportError(
            "PostgreSQL import could not be confirmed. Check the connection, permissions, and public.agency in DBeaver before retrying."
        ) from None

    # 只有离开连接块、完成提交后，才能返回成功，避免提前打印成功信息。
    return ImportResult(EXPECTED_DATABASE, len(actual_rows))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lesson 3.2: preview agency.txt; --apply creates and imports public.agency.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="GTFS directory; defaults to this project's data/raw/MBTA_GTFS.")
    parser.add_argument("--apply", action="store_true", help="Write the new table to the local transitmind database.")
    args = parser.parse_args(argv)
    try:
        snapshot = read_agencies(args.source)
        # 不加 --apply 时，不加载数据库密码、不连接数据库，只预览真实文件。
        result = import_agencies(load_database_settings(), snapshot) if args.apply else None
    except (AgencyImportError, DatabaseCheckError) as error:
        parser.exit(2, f"Error: {error}\n")

    mode = "agency_import (real database write; no LLM)" if result else "agency_preview (local file only; no database or LLM)"
    print(f"Mode: {mode}")
    print(f"Source: {snapshot.source_path}")
    print("Target: transitmind.public.agency")
    print(f"Source rows: {len(snapshot.agencies)}")
    print(f"Source SHA256: {snapshot.sha256}")
    for agency in snapshot.agencies:
        print(f"Agency: {agency.agency_id} | {agency.agency_name}")
    if result:
        print(f"Database: {result.database_name}")
        print(f"Verified rows: {result.row_count}")
        print("Committed: yes. Only public.agency was created and imported.")
    else:
        print("Preview only. No tables were created. Add --apply to create and import public.agency.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
