import csv
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from transitmind.gtfs_inventory import InventoryError, inspect_file, inspect_gtfs, main, write_report


class GtfsInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "feed"
        self.root.mkdir()
        files = {
            "agency.txt": (["agency_id", "agency_name"], [["A1", "Demo"]]),
            "routes.txt": (["route_id", "agency_id"], [["R1", "A1"]]),
            "trips.txt": (["trip_id", "route_id", "service_id"], [["T1", "R1", "S1"]]),
            "stops.txt": (["stop_id", "stop_name", "parent_station"], [["0001", "Main, Street", ""]]),
            "stop_times.txt": (["trip_id", "stop_id", "arrival_time"], [["T1", "0001", "26:05:00"]]),
            "calendar.txt": (["service_id", "start_date", "end_date"], [["S1", "20260715", "20260905"]]),
            "calendar_dates.txt": (["service_id", "date", "exception_type"], [["S1", "20260716", "2"]]),
            "feed_info.txt": (["feed_start_date", "feed_end_date"], [["20260715", "20260905"]]),
        }
        for name, (columns, rows) in files.items():
            self.write_csv(name, columns, rows)

    def write_csv(self, name: str, columns: list[str], rows: list[list[str]], encoding: str = "utf-8") -> None:
        with (self.root / name).open("w", encoding=encoding, newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)

    def test_inspects_core_tables_and_separates_extensions(self) -> None:
        (self.root / "shapes.txt").write_text("not parsed in this lesson", encoding="utf-8")
        inventory = inspect_gtfs(self.root)
        self.assertEqual(len(inventory.tables), 7)
        self.assertEqual(inventory.other_txt_files, ("shapes.txt",))
        stops = next(item for item in inventory.tables if item.file_name == "stops.txt")
        self.assertEqual(stops.row_count, 1)
        self.assertEqual(stops.first_row["stop_id"], "0001")
        self.assertEqual(stops.first_row["stop_name"], "Main, Street")
        self.assertEqual(stops.first_row["parent_station"], "")
        self.assertEqual(inventory.feed_info.first_row["feed_end_date"], "20260905")

    def test_bom_quoted_newlines_blank_lines_and_unicode(self) -> None:
        name = "\u4e2d\u592e\u7ad9\nPlatform 1"
        self.write_csv("stops.txt", ["stop_id", "stop_name"], [["0001", name], [], ["0002", "Second"]], "utf-8-sig")
        summary = inspect_file(self.root / "stops.txt")
        self.assertEqual(summary.columns, ("stop_id", "stop_name"))
        self.assertEqual(summary.row_count, 2)
        self.assertEqual(summary.first_row["stop_name"], name)

    def test_service_day_time_stays_a_string(self) -> None:
        summary = inspect_file(self.root / "stop_times.txt")
        self.assertEqual(summary.first_row["arrival_time"], "26:05:00")

    def test_fingerprint_is_repeatable_and_changes_with_core_data(self) -> None:
        first = inspect_gtfs(self.root).core_snapshot_sha256
        self.assertEqual(first, inspect_gtfs(self.root).core_snapshot_sha256)
        (self.root / "extension.txt").write_text("unused", encoding="utf-8")
        self.assertEqual(first, inspect_gtfs(self.root).core_snapshot_sha256)
        self.write_csv("stops.txt", ["stop_id", "stop_name"], [["0001", "Renamed"]])
        self.assertNotEqual(first, inspect_gtfs(self.root).core_snapshot_sha256)

    def test_missing_core_file_and_non_directory_are_rejected(self) -> None:
        with self.assertRaisesRegex(InventoryError, "directory"):
            inspect_gtfs(self.root / "missing")
        (self.root / "trips.txt").unlink()
        with self.assertRaisesRegex(InventoryError, "trips.txt"):
            inspect_gtfs(self.root)

    def test_either_calendar_file_is_supported_but_one_is_required(self) -> None:
        (self.root / "calendar.txt").unlink()
        self.assertEqual(len(inspect_gtfs(self.root).tables), 6)
        (self.root / "calendar_dates.txt").unlink()
        with self.assertRaisesRegex(InventoryError, "calendar"):
            inspect_gtfs(self.root)
        self.write_csv("calendar.txt", ["service_id"], [["S1"]])
        self.assertEqual(len(inspect_gtfs(self.root).tables), 6)

    def test_empty_duplicate_and_missing_headers_are_rejected(self) -> None:
        for content in ("", ",stop_name\n", "stop_id,stop_id\n"):
            with self.subTest(content=content):
                (self.root / "stops.txt").write_text(content, encoding="utf-8")
                with self.assertRaises(InventoryError):
                    inspect_file(self.root / "stops.txt")

    def test_mismatched_rows_invalid_quotes_and_encoding_are_rejected(self) -> None:
        for content in (b"stop_id,stop_name\n0001\n", b'stop_id,stop_name\n0001,"unclosed\n', b"stop_id\n\xff\n"):
            with self.subTest(content=content):
                (self.root / "stops.txt").write_bytes(content)
                with self.assertRaises(InventoryError):
                    inspect_file(self.root / "stops.txt")

    def test_changed_file_is_rejected(self) -> None:
        before = SimpleNamespace(st_size=100, st_mtime_ns=1)
        after = SimpleNamespace(st_size=100, st_mtime_ns=2)
        with patch.object(Path, "stat", side_effect=[before, after]):
            with self.assertRaisesRegex(InventoryError, "changed during inspection"):
                inspect_file(self.root / "stops.txt")

    def test_report_is_json_and_never_overwrites_existing_files(self) -> None:
        inventory = inspect_gtfs(self.root)
        destination = Path(self.directory.name) / "reports" / "inventory.json"
        write_report(inventory, destination)
        content = destination.read_text(encoding="utf-8")
        self.assertEqual(json.loads(content)["core_snapshot_sha256"], inventory.core_snapshot_sha256)
        with self.assertRaisesRegex(InventoryError, "already exists"):
            write_report(inventory, destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), content)
        with self.assertRaisesRegex(InventoryError, "outside"):
            write_report(inventory, self.root / "stops.txt")

    def test_cli_text_json_and_report_do_not_call_external_services(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--source", str(self.root)]), 0)
        self.assertIn("no LLM or database", output.getvalue())
        destination = Path(self.directory.name) / "report.json"
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--source", str(self.root), "--json", "--report", str(destination)]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["tables"]), 7)
        self.assertTrue(destination.is_file())

    def test_cli_reports_missing_files_with_nonzero_exit(self) -> None:
        (self.root / "agency.txt").unlink()
        error = io.StringIO()
        with redirect_stderr(error):
            with self.assertRaises(SystemExit) as result:
                main(["--source", str(self.root)])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("agency.txt", error.getvalue())


if __name__ == "__main__":
    unittest.main()
