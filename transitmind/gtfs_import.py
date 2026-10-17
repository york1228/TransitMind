"""Lesson 3: import configured GTFS tables with explicit table specifications."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

import psycopg
from psycopg import sql

from .gtfs_inventory import DEFAULT_SOURCE
from .database_config import DatabaseCheckError, DatabaseSettings, EXPECTED_DATABASE, EXPECTED_QUERY_USER, load_database_settings


@dataclass(frozen=True)
class ColumnSpec:
    # 描述一个数据库字段，不是存放一条业务记录。
    name: str
    pg_type: str = "text"
    required: bool = False


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[tuple[str, str, str], ...] = ()
    # 有些关系不能用一个普通外键表达，但导入前必须先存在这些父表。
    required_tables: tuple[str, ...] = ()
    # 大表只保留少量预览，正式导入时边读边写；indexes 用于常见筛选字段。
    streaming: bool = False
    indexes: tuple[tuple[str, ...], ...] = ()


# 表结构放在配置中，读取、建表、插入共用同一套代码；不创建 401 个 Routes 类。
# 小表沿用列表导入，大表按 streaming 配置走生成器和 COPY 数据流。
TABLE_SPECS = {
    "routes": TableSpec(
        name="routes",
        columns=(
            ColumnSpec("route_id", required=True), ColumnSpec("agency_id"),
            ColumnSpec("route_short_name"), ColumnSpec("route_long_name"), ColumnSpec("route_desc"),
            ColumnSpec("route_type", "integer", True), ColumnSpec("route_url"),
            ColumnSpec("route_color"), ColumnSpec("route_text_color"),
            ColumnSpec("route_sort_order", "integer"),
            ColumnSpec("route_fare_class"), ColumnSpec("line_id"),
            ColumnSpec("listed_route"), ColumnSpec("network_id"),
        ),
        primary_key=("route_id",),
        # 每项依次表示：本表字段、引用的表、引用的字段。
        foreign_keys=(("agency_id", "agency", "agency_id"),),
    ),
    "calendar": TableSpec(
        name="calendar",
        columns=(
            ColumnSpec("service_id", required=True),
            ColumnSpec("monday", "integer", True), ColumnSpec("tuesday", "integer", True),
            ColumnSpec("wednesday", "integer", True), ColumnSpec("thursday", "integer", True),
            ColumnSpec("friday", "integer", True), ColumnSpec("saturday", "integer", True),
            ColumnSpec("sunday", "integer", True),
            # GTFS 使用 YYYYMMDD；先保留原始文本，后续再决定日期业务类型。
            ColumnSpec("start_date", required=True), ColumnSpec("end_date", required=True),
        ),
        primary_key=("service_id",),
    ),
    "calendar_dates": TableSpec(
        name="calendar_dates",
        columns=(
            ColumnSpec("service_id", required=True), ColumnSpec("date", required=True),
            ColumnSpec("exception_type", "integer", True), ColumnSpec("holiday_name"),
        ),
        primary_key=("service_id", "date"),
        # service_id 可能只出现在例外日文件中，所以这里不硬连 calendar 外键。
    ),
    "stops": TableSpec(
        name="stops",
        columns=(
            ColumnSpec("stop_id", required=True), ColumnSpec("stop_code"),
            ColumnSpec("stop_name", required=True), ColumnSpec("stop_desc"),
            ColumnSpec("platform_code"), ColumnSpec("platform_name"),
            ColumnSpec("stop_lat", "double precision"), ColumnSpec("stop_lon", "double precision"),
            ColumnSpec("zone_id"), ColumnSpec("stop_address"), ColumnSpec("stop_url"),
            ColumnSpec("level_id"), ColumnSpec("location_type", "integer", True),
            ColumnSpec("parent_station"), ColumnSpec("wheelchair_boarding", "integer", True),
            ColumnSpec("municipality"), ColumnSpec("on_street"), ColumnSpec("at_street"),
            ColumnSpec("vehicle_type", "integer"),
        ),
        primary_key=("stop_id",),
        # parent_station 为空时表示顶层站点；有值时引用本表中的父站点。
        foreign_keys=(("parent_station", "stops", "stop_id"),),
    ),
    "trips": TableSpec(
        name="trips",
        columns=(
            ColumnSpec("route_id", required=True), ColumnSpec("service_id", required=True),
            ColumnSpec("trip_id", required=True), ColumnSpec("trip_headsign"),
            ColumnSpec("trip_short_name"), ColumnSpec("direction_id", "integer"),
            ColumnSpec("block_id"), ColumnSpec("shape_id"),
            ColumnSpec("wheelchair_accessible", "integer"), ColumnSpec("trip_route_type", "integer"),
            ColumnSpec("route_pattern_id"), ColumnSpec("bikes_allowed", "integer"),
        ),
        primary_key=("trip_id",),
        foreign_keys=(("route_id", "routes", "route_id"),),
        # service_id 对应 calendar 与 calendar_dates 的并集，不能用单一外键表达。
        required_tables=("calendar", "calendar_dates"),
    ),
    "stop_times": TableSpec(
        name="stop_times",
        columns=(
            ColumnSpec("trip_id", required=True),
            # GTFS 服务日可超过 24 小时，例如 25:10:00；保留文本而不用普通 TIME。
            ColumnSpec("arrival_time"), ColumnSpec("departure_time"),
            ColumnSpec("stop_id", required=True), ColumnSpec("stop_sequence", "integer", True),
            ColumnSpec("stop_headsign"), ColumnSpec("pickup_type", "integer"),
            ColumnSpec("drop_off_type", "integer"), ColumnSpec("timepoint", "integer"),
            ColumnSpec("checkpoint_id"), ColumnSpec("continuous_pickup", "integer"),
            ColumnSpec("continuous_drop_off", "integer"),
        ),
        # 同一班次可以重复经过一个站点，因此用班次 ID + 停站顺序标识一行。
        primary_key=("trip_id", "stop_sequence"),
        foreign_keys=(("trip_id", "trips", "trip_id"), ("stop_id", "stops", "stop_id")),
        streaming=True,
        # 联合主键已支持按 trip_id 查询；额外索引支持按 stop_id 查停站记录。
        indexes=(("stop_id",),),
    ),
}


class GtfsImportError(ValueError):
    """A local import diagnostic without credentials or raw driver errors."""


def iter_table_rows(spec: TableSpec, source: Path) -> Iterator[dict]:
    # yield 每次交出一行；调用方继续迭代时才读取下一行，不积攒整个文件。
    path = source.resolve() / f"{spec.name}.txt"
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            names = [column.name for column in spec.columns]
            if len(reader.fieldnames or []) != len(names) or set(reader.fieldnames or []) != set(names):
                raise GtfsImportError(f"{spec.name}.txt columns do not match its TableSpec.")
            for record in reader:
                if None in record or any(value is None for value in record.values()):
                    raise GtfsImportError(f"{spec.name}.txt: column mismatch at line {reader.line_num}.")
                values = {}
                for column in spec.columns:
                    value = record[column.name]
                    if column.required and not value.strip():
                        raise GtfsImportError(f"Line {reader.line_num}: {column.name} must not be empty.")
                    # ID、名称和业务编码保留文本；空字符串转 NULL，数值字段按配置转换。
                    try:
                        if not value:
                            values[column.name] = None
                        elif column.pg_type == "integer":
                            values[column.name] = int(value)
                        elif column.pg_type == "double precision":
                            values[column.name] = float(value)
                        else:
                            values[column.name] = value
                    except ValueError:
                        raise GtfsImportError(f"Line {reader.line_num}: {column.name} has an invalid {column.pg_type} value.") from None
                yield values
    except OSError:
        raise GtfsImportError(f"Cannot read {spec.name}.txt in the selected GTFS directory.") from None
    except (UnicodeError, csv.Error):
        raise GtfsImportError(f"{spec.name}.txt contains invalid UTF-8 or CSV.") from None


def read_table(spec: TableSpec, source: Path, *, limit: int | None = None) -> tuple[Path, list[dict], str]:
    path = source.resolve() / f"{spec.name}.txt"
    # 小表读取完整列表，大表预览只取前几行；二者共用同一个字段转换流程。
    records = iter_table_rows(spec, source)
    rows = list(records if limit is None else islice(records, limit))
    if not rows:
        raise GtfsImportError(f"{spec.name}.txt contains no records.")
    try:
        # file_digest 分块计算文件指纹，不把整个大文件装进内存。
        with path.open("rb") as handle:
            sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        raise GtfsImportError(f"Cannot read {spec.name}.txt in the selected GTFS directory.") from None
    return path, rows, sha256


def validate_trip_service_ids(source: Path, rows: list[dict]) -> int:
    """确认 trips.service_id 能在基础日历或例外日文件中找到。"""
    _, calendar_rows, _ = read_table(TABLE_SPECS["calendar"], source)
    _, date_rows, _ = read_table(TABLE_SPECS["calendar_dates"], source)
    known_ids = {row["service_id"] for row in calendar_rows}
    known_ids.update(row["service_id"] for row in date_rows)
    missing = sorted({row["service_id"] for row in rows} - known_ids)
    if missing:
        preview = ", ".join(missing[:5])
        suffix = "..." if len(missing) > 5 else ""
        raise GtfsImportError(f"trips.txt has service_id values absent from calendar/calendar_dates: {preview}{suffix}")
    return len(known_ids)


def build_statements(spec: TableSpec) -> tuple[sql.Composable, sql.Composable]:
    # 表名/列名用 Identifier，业务值用 Placeholder；不能用同一种拼接方式处理二者。
    # pg_type 只来自上面代码中的可信配置，不接收模型提供的类型或 DDL。
    columns = [sql.SQL("{} {}{}").format(
        sql.Identifier(column.name), sql.SQL(column.pg_type),
        sql.SQL(" NOT NULL" if column.required else ""),
    ) for column in spec.columns]
    columns.append(sql.SQL("PRIMARY KEY ({})").format(
        sql.SQL(", ").join(sql.Identifier(name) for name in spec.primary_key),
    ))
    for local, parent_table, parent_column in spec.foreign_keys:
        columns.append(sql.SQL("FOREIGN KEY ({}) REFERENCES {} ({})").format(
            sql.Identifier(local), sql.Identifier("public", parent_table), sql.Identifier(parent_column),
        ))
    table = sql.Identifier("public", spec.name)
    create = sql.SQL("CREATE TABLE {} ({})").format(table, sql.SQL(", ").join(columns))
    insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        table, sql.SQL(", ").join(sql.Identifier(column.name) for column in spec.columns),
        sql.SQL(", ").join(sql.Placeholder(column.name) for column in spec.columns),
    )
    return create, insert


def import_table(settings: DatabaseSettings, spec: TableSpec, rows: Sequence[dict] | Iterator[dict]) -> int:
    if settings.host not in {"localhost", "127.0.0.1", "::1"} or settings.database != EXPECTED_DATABASE:
        raise GtfsImportError("Only the local transitmind database is allowed.")
    streaming = not isinstance(rows, Sequence)
    if not streaming and not rows:
        raise GtfsImportError("Cannot import an empty table.")
    create, insert = build_statements(spec)
    # COPY 导入整份大文件可能超过 5 秒；这里只放宽导入连接，Agent 查询仍限 5 秒。
    timeout_ms = 600000 if streaming else 5000
    try:
        # 明确的 --apply 才进入这里；建表、写入、授权在同一事务中，失败一起回滚。
        with psycopg.connect(
            host=settings.host, port=settings.port, dbname=settings.database,
            user=settings.user, password=settings.password, connect_timeout=5, autocommit=False,
            options=f"-c statement_timeout={timeout_ms} -c lock_timeout=5000", application_name="transitmind_gtfs_import",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database(), to_regclass(%s)", (f"public.{spec.name}",))
                target = cursor.fetchone()
                if target is None or target[0] != EXPECTED_DATABASE:
                    raise GtfsImportError("The connected database is not transitmind.")
                if target[1] is not None:
                    raise GtfsImportError(f"public.{spec.name} already exists; nothing will be overwritten.")
                # 普通外键由 PostgreSQL 建表时声明；这里先检查独立父表，给出更清楚的导入顺序提示。
                parent_tables = {
                    parent for _, parent, _ in spec.foreign_keys if parent != spec.name
                } | set(spec.required_tables)
                for parent_table in sorted(parent_tables):
                    cursor.execute("SELECT to_regclass(%s)", (f"public.{parent_table}",))
                    if cursor.fetchone()[0] is None:
                        raise GtfsImportError(
                            f"Required parent table public.{parent_table} is missing; import it first."
                        )
                cursor.execute(create)
                if isinstance(rows, Sequence):
                    cursor.executemany(insert, rows)
                    written = len(rows)
                else:
                    copy_statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                        sql.Identifier("public", spec.name),
                        sql.SQL(", ").join(sql.Identifier(column.name) for column in spec.columns),
                    )
                    written = 0
                    # 一条 COPY 命令接收整条数据流，驱动负责缓冲发送；不是每行执行一条 INSERT。
                    with cursor.copy(copy_statement) as copy:
                        for record in rows:
                            copy.write_row(tuple(record[column.name] for column in spec.columns))
                            written += 1
                            if written % 100000 == 0:
                                print(f"COPY rows sent: {written:,} (not committed yet)", flush=True)
                    if written == 0:
                        raise GtfsImportError("Cannot import an empty table.")
                    print(f"COPY complete: {written:,} rows. Creating indexes...", flush=True)
                # PostgreSQL 不会自动给外键字段建索引，按 TableSpec 配置补充常用索引。
                for index_columns in spec.indexes:
                    cursor.execute(sql.SQL("CREATE INDEX {} ON {} ({})").format(
                        sql.Identifier(f"{spec.name}_{'_'.join(index_columns)}_idx"),
                        sql.Identifier("public", spec.name),
                        sql.SQL(", ").join(sql.Identifier(name) for name in index_columns),
                    ))
                if streaming:
                    # 更新统计信息，方便数据库为后续停站查询选择合适的执行计划。
                    cursor.execute(sql.SQL("ANALYZE {}").format(sql.Identifier("public", spec.name)))
                # 主键、外键和 NOT NULL 由 PostgreSQL 保证；这里只确认写入数量。
                cursor.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier("public", spec.name)))
                count = cursor.fetchone()[0]
                if count != written:
                    raise GtfsImportError("Imported row count differs from the source; rolling back.")
                cursor.execute(sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                    sql.Identifier("public", spec.name), sql.Identifier(EXPECTED_QUERY_USER),
                ))
    except psycopg.errors.ForeignKeyViolation:
        raise GtfsImportError("A referenced ID is missing in a parent table; the import was rolled back.") from None
    except psycopg.errors.DuplicateTable:
        raise GtfsImportError(f"public.{spec.name} already exists; nothing was overwritten.") from None
    except psycopg.Error:
        raise GtfsImportError(
            "Import could not be confirmed. Check permissions, constraints and the table in Navicat before retrying."
        ) from None
    # 离开连接块并提交成功后，才返回成功数量。
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview a configured GTFS table; --apply imports and grants SELECT.")
    parser.add_argument("--table", choices=TABLE_SPECS, default="routes")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--apply", action="store_true", help="Create the new table, import records and grant reader SELECT.")
    args = parser.parse_args(argv)
    spec = TABLE_SPECS[args.table]
    try:
        path, rows, sha256 = read_table(spec, args.source, limit=5 if spec.streaming else None)
        service_id_count = validate_trip_service_ids(args.source, rows) if spec.name == "trips" else None
        # 预览不加载密码、不连接数据库；写入继续用原来的导入账号。
        count = None
        if args.apply:
            print(f"Importing public.{spec.name} with {'streaming COPY' if spec.streaming else 'INSERT'}...", flush=True)
            # 大表预览的 5 行只用于展示；正式导入重新打开文件，从第一行读到末尾。
            import_rows = iter_table_rows(spec, args.source) if spec.streaming else rows
            count = import_table(load_database_settings(), spec, import_rows)
    except (GtfsImportError, DatabaseCheckError) as error:
        parser.exit(2, f"Error: {error}\n")
    print(f"Mode: gtfs_{'import (real database write)' if args.apply else 'preview (local file only)'}; no LLM")
    print(f"Source: {path}")
    print(f"Target: transitmind.public.{spec.name}")
    if spec.streaming and count is None:
        print(f"Preview rows: {len(rows)} (first rows only; total not counted) | Columns: {len(spec.columns)}")
    else:
        print(f"Source rows: {count if spec.streaming else len(rows)} | Columns: {len(spec.columns)}")
    print(f"Source SHA256: {sha256}")
    if spec.streaming:
        for row in rows:
            print(f"Preview row: {json.dumps(row, ensure_ascii=False)}")
    if service_id_count is not None:
        print(f"Validated service IDs against calendar + calendar_dates: {service_id_count}")
    if "agency_id" in rows[0]:
        print(f"Rows by agency_id: {dict(sorted(Counter(str(row['agency_id']) for row in rows).items()))}")
    if count is None:
        print("Preview only. Add --apply to create, import and grant SELECT. Existing tables are never overwritten.")
    else:
        print(f"Verified rows: {count}")
        print(f"Committed: yes. Granted SELECT on public.{spec.name} to {EXPECTED_QUERY_USER}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
