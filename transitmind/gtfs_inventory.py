"""Inspect an extracted GTFS snapshot without calling a model or a database."""

import argparse
import csv
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path


# 从代码所在位置定位项目根目录，默认数据路径不受终端当前目录影响。
DEFAULT_SOURCE = Path(__file__).resolve().parent.parent / "data" / "raw" / "MBTA_GTFS"
CORE_FILES = (
    "agency.txt", "routes.txt", "trips.txt", "stops.txt", "stop_times.txt",
    "calendar.txt", "calendar_dates.txt",
)


class InventoryError(ValueError):
    """A file cannot be safely summarized for this lesson."""


@dataclass(frozen=True)
class FileInventory:
    # 文件清单
    file_name: str
    columns: tuple[str, ...]
    row_count: int
    size_bytes: int
    sha256: str
    first_row: dict[str, str]


@dataclass(frozen=True)
class GtfsInventory:
    source: str
    tables: tuple[FileInventory, ...]
    feed_info: FileInventory | None
    other_txt_files: tuple[str, ...]
    core_snapshot_sha256: str


def inspect_file(path: Path) -> FileInventory:
    before = path.stat()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, strict=True)# 打开文件
            columns = next(reader, [])# 取出字段名
            if not columns or any(not name.strip() for name in columns):
                raise InventoryError(f"{path.name}: missing or empty column names.")
            if len(columns) != len(set(columns)):
                raise InventoryError(f"{path.name}: duplicate column names.")

            row_count = 0
            first_row: dict[str, str] = {}
            for row in reader:
                if not row:
                    continue
                if len(row) != len(columns):
                    raise InventoryError(f"{path.name}: column count mismatch near line {reader.line_num}.")
                if row_count == 0:
                    first_row = dict(zip(columns, row))
                row_count += 1
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except (csv.Error, UnicodeError):
        raise InventoryError(f"{path.name}: invalid UTF-8 or CSV structure.") from None

    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise InventoryError(f"{path.name}: changed during inspection; use a fixed snapshot.")
    return FileInventory(path.name, tuple(columns), row_count, before.st_size, digest, first_row)


def inspect_gtfs(source: Path) -> GtfsInventory:
    source = source.resolve()
    if not source.is_dir():
        raise InventoryError("The source must be an extracted GTFS directory, not a ZIP file.")
    missing = [name for name in CORE_FILES[:5] if not (source / name).is_file()]
    if missing:
        raise InventoryError("Missing lesson core files: " + ", ".join(missing))
    if not any((source / name).is_file() for name in CORE_FILES[5:]):
        raise InventoryError("Provide calendar.txt, calendar_dates.txt, or both.")

    tables = tuple(inspect_file(source / name) for name in CORE_FILES if (source / name).is_file())
    feed_path = source / "feed_info.txt"
    feed_info = inspect_file(feed_path) if feed_path.is_file() else None
    selected = tables + ((feed_info,) if feed_info is not None else ())
    selected_names = {item.file_name for item in selected}
    other_files = tuple(sorted(
        path.name for path in source.iterdir()
        if path.is_file() and path.suffix.lower() == ".txt" and path.name not in selected_names
    ))
    # The fingerprint covers selected core files and feed metadata, not ignored extensions.
    manifest = [{"file_name": item.file_name, "sha256": item.sha256} for item in selected]
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest()
    return GtfsInventory(str(source), tables, feed_info, other_files, fingerprint)


def write_report(inventory: GtfsInventory, destination: Path) -> None:
    destination = destination.resolve()
    if destination.is_relative_to(Path(inventory.source)):
        raise InventoryError("Write reports outside the source directory; original GTFS files stay unchanged.")
    payload = json.dumps(asdict(inventory), ensure_ascii=False, indent=2) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(payload)
    except FileExistsError:
        raise InventoryError("The report already exists; choose a new output path.") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lesson 3.1: inspect local GTFS files, without an LLM or database.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="GTFS directory; defaults to this project's data/raw/MBTA_GTFS.")
    parser.add_argument("--report", type=Path, help="Write a new JSON report outside the source directory.")
    parser.add_argument("--json", action="store_true", help="Print the full inventory as JSON.")
    args = parser.parse_args(argv)
    try:
        inventory = inspect_gtfs(args.source)
        if args.report is not None:
            write_report(inventory, args.report)
    except InventoryError as error:
        parser.exit(2, f"Error: {error}\n")
    except OSError:
        parser.exit(2, "Error: cannot access input files or write the report; check paths and permissions.\n")

    if args.json:
        print(json.dumps(asdict(inventory), ensure_ascii=False, indent=2))
        return 0
    print("Mode: gtfs_inventory (local files only; no LLM or database)")
    print(f"Source: {inventory.source}")
    print(f"{'File':<22} {'Rows':>12} {'Columns':>8}")
    for item in inventory.tables:
        print(f"{item.file_name:<22} {item.row_count:>12,} {len(item.columns):>8}")
    if inventory.feed_info is not None:
        feed = inventory.feed_info.first_row
        print(f"Feed dates: {feed.get('feed_start_date', 'not supplied')} - {feed.get('feed_end_date', 'not supplied')}")
        print(f"Feed version: {feed.get('feed_version', 'not supplied')}")
    print(f"Other .txt files not inspected: {len(inventory.other_txt_files)}")
    print(f"Core snapshot SHA256: {inventory.core_snapshot_sha256}")
    if args.report is not None:
        print(f"Report: {args.report.resolve()}")
    print("This is a file inventory, not a complete GTFS validation or database import.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
