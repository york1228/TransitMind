import csv
import hashlib
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, astuple
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

import psycopg

from transitmind.agency_import import (
    AGENCY_COLUMNS,
    CREATE_SQL,
    DATABASE_SQL,
    EXISTS_SQL,
    IMPORT_OPTIONS,
    INSERT_SQL,
    REQUIRED_VALUES,
    VERIFY_SQL,
    Agency,
    AgencyImportError,
    AgencySnapshot,
    ImportResult,
    import_agencies,
    main,
    read_agencies,
)
from transitmind.postgres_check import DatabaseCheckError, DatabaseSettings


FAKE_PASSWORD = "fake-agency-password-not-real"
DEMO_AGENCY = Agency("001", "Demo, Transit", "https://example.org", "America/New_York", "EN", None, None)


class AgencyFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "agency.txt"

    def write_agencies(self, rows=None, columns=AGENCY_COLUMNS, encoding="utf-8"):
        with self.path.open("w", encoding=encoding, newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows([astuple(DEMO_AGENCY)] if rows is None else rows)

    def test_parses_values_and_hash_without_changing_the_file(self):
        self.write_agencies()
        before = self.path.read_bytes()
        snapshot = read_agencies(self.root)
        self.assertEqual(snapshot.source_path, self.path)
        self.assertEqual(snapshot.agencies, (DEMO_AGENCY,))
        self.assertEqual(snapshot.sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(snapshot.agencies[0].agency_id, "001")
        self.assertIsNone(snapshot.agencies[0].agency_phone)

    def test_reordered_headers_bom_and_quoted_chinese_newline(self):
        name = "\u4ea4\u901a\u516c\u53f8, Transit\nBranch"
        agency = Agency("A-01", name, "https://example.org", "Asia/Shanghai", None, "+86-001", None)
        values = asdict(agency)
        columns = tuple(reversed(AGENCY_COLUMNS))
        self.write_agencies([[values[column] for column in columns]], columns, "utf-8-sig")
        self.assertEqual(read_agencies(self.root).agencies, (agency,))

    def test_optional_zero_text_is_not_changed_to_null(self):
        row = list(astuple(DEMO_AGENCY))
        row[5] = "0"
        self.write_agencies([row])
        self.assertEqual(read_agencies(self.root).agencies[0].agency_phone, "0")

    def test_empty_missing_extra_and_duplicate_headers_are_rejected(self):
        for columns in ((), AGENCY_COLUMNS[:-1], AGENCY_COLUMNS + ("agency_email",),
                        AGENCY_COLUMNS[:-1] + ("agency_id",)):
            with self.subTest(columns=columns):
                self.write_agencies([], columns)
                with self.assertRaisesRegex(AgencyImportError, "seven columns"):
                    read_agencies(self.root)

    def test_missing_short_and_extra_row_values_are_rejected(self):
        for row in (astuple(DEMO_AGENCY)[:-1], astuple(DEMO_AGENCY) + ("extra",)):
            with self.subTest(row=row):
                self.write_agencies([row])
                with self.assertRaisesRegex(AgencyImportError, "column mismatch"):
                    read_agencies(self.root)

    def test_empty_required_values_and_duplicate_ids_are_rejected(self):
        for column in REQUIRED_VALUES:
            with self.subTest(column=column):
                row = list(astuple(DEMO_AGENCY))
                row[AGENCY_COLUMNS.index(column)] = " "
                self.write_agencies([row])
                with self.assertRaisesRegex(AgencyImportError, f"required {column}"):
                    read_agencies(self.root)
        self.write_agencies([astuple(DEMO_AGENCY), astuple(DEMO_AGENCY)])
        with self.assertRaisesRegex(AgencyImportError, "duplicate agency_id"):
            read_agencies(self.root)

    def test_missing_empty_invalid_utf8_and_invalid_csv_are_rejected(self):
        with self.assertRaisesRegex(AgencyImportError, "Cannot read"):
            read_agencies(self.root)
        self.write_agencies([])
        with self.assertRaisesRegex(AgencyImportError, "no agency records"):
            read_agencies(self.root)
        for content in (b"\xff", (",".join(AGENCY_COLUMNS) + '\n001,"unfinished').encode()):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaisesRegex(AgencyImportError, "invalid UTF-8 or CSV"):
                    read_agencies(self.root)


class AgencyDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.settings = DatabaseSettings("localhost", 5432, "transitmind", "postgres", FAKE_PASSWORD)
        self.agency = Agency("001", "O'Reilly; DROP TABLE agency; --", "https://example.org", "America/New_York", None, None, None)
        self.snapshot = AgencySnapshot(Path("agency.txt"), (self.agency,), "test-hash")

    def prepare_connection(self, connect):
        connection = connect.return_value.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("transitmind",), (None,)]
        cursor.fetchall.return_value = [astuple(self.agency)]
        return connection, cursor

    def test_fixed_sql_bound_values_and_transactional_connection(self):
        with patch("transitmind.agency_import.psycopg.connect") as connect:
            connection, cursor = self.prepare_connection(connect)
            result = import_agencies(self.settings, self.snapshot)
        self.assertEqual(result, ImportResult("transitmind", 1))
        connect.assert_called_once_with(
            host="localhost", port=5432, dbname="transitmind", user="postgres", password=FAKE_PASSWORD,
            connect_timeout=5, autocommit=False, options=IMPORT_OPTIONS,
            application_name="transitmind_agency_import",
        )
        self.assertEqual(cursor.execute.call_args_list,
                         [call(DATABASE_SQL), call(EXISTS_SQL), call(CREATE_SQL), call(VERIFY_SQL)])
        cursor.executemany.assert_called_once_with(INSERT_SQL, [asdict(self.agency)])
        self.assertNotIn(self.agency.agency_name, INSERT_SQL)
        self.assertNotIn("IF NOT EXISTS", CREATE_SQL)
        self.assertIn("agency_id TEXT PRIMARY KEY", CREATE_SQL)
        connection.cursor.return_value.__exit__.assert_called_once_with(None, None, None)
        connect.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_verification_ignores_row_order_and_compares_all_seven_values(self):
        second = Agency("002", "Second", "https://example.org", "America/New_York", "EN", "001-123", None)
        snapshot = AgencySnapshot(Path("agency.txt"), (second, self.agency), "test-hash")
        with patch("transitmind.agency_import.psycopg.connect") as connect:
            _, cursor = self.prepare_connection(connect)
            cursor.fetchall.return_value = [astuple(self.agency), astuple(second)]
            self.assertEqual(import_agencies(self.settings, snapshot).row_count, 2)
            cursor.fetchone.side_effect = [("transitmind",), (None,)]
            cursor.fetchall.return_value = [astuple(second), astuple(self.agency)]
            self.assertEqual(import_agencies(self.settings, snapshot).row_count, 2)

    def test_wrong_config_or_empty_snapshot_never_connects(self):
        cases = [
            (DatabaseSettings("example.org", 5432, "transitmind", "postgres", FAKE_PASSWORD), self.snapshot),
            (DatabaseSettings("localhost", 5432, "postgres", "postgres", FAKE_PASSWORD), self.snapshot),
            (self.settings, AgencySnapshot(Path("agency.txt"), (), "empty")),
        ]
        for settings, snapshot in cases:
            with self.subTest(settings=settings):
                with patch("transitmind.agency_import.psycopg.connect") as connect:
                    with self.assertRaises(AgencyImportError):
                        import_agencies(settings, snapshot)
                connect.assert_not_called()

    def test_wrong_database_or_existing_relation_stops_before_writes(self):
        cases = ([None], [("postgres",)], [("transitmind",), ("agency",)], [("transitmind",), None])
        for answers in cases:
            with self.subTest(answers=answers):
                with patch("transitmind.agency_import.psycopg.connect") as connect:
                    _, cursor = self.prepare_connection(connect)
                    cursor.fetchone.side_effect = answers
                    with self.assertRaises(AgencyImportError):
                        import_agencies(self.settings, self.snapshot)
                self.assertNotIn(call(CREATE_SQL), cursor.execute.call_args_list)
                cursor.executemany.assert_not_called()
                self.assertIs(connect.return_value.__exit__.call_args.args[0], AgencyImportError)

    def test_mismatched_readback_leaves_connection_context_with_an_error(self):
        wrong_value = list(astuple(self.agency))
        wrong_value[3] = "wrong-timezone"
        for rows in ([], [tuple(wrong_value)], [astuple(self.agency), astuple(self.agency)]):
            with self.subTest(rows=rows):
                with patch("transitmind.agency_import.psycopg.connect") as connect:
                    _, cursor = self.prepare_connection(connect)
                    cursor.fetchall.return_value = rows
                    with self.assertRaisesRegex(AgencyImportError, "rolled back"):
                        import_agencies(self.settings, self.snapshot)
                self.assertIs(connect.return_value.__exit__.call_args.args[0], AgencyImportError)

    def test_insert_failure_is_redacted_and_error_reaches_transaction_context(self):
        with patch("transitmind.agency_import.psycopg.connect") as connect:
            _, cursor = self.prepare_connection(connect)
            cursor.executemany.side_effect = psycopg.Error(f"private insert diagnostic {FAKE_PASSWORD}")
            with self.assertRaises(AgencyImportError) as raised:
                import_agencies(self.settings, self.snapshot)
        self.assertNotIn(FAKE_PASSWORD, str(raised.exception))
        self.assertNotIn("private insert diagnostic", str(raised.exception))
        self.assertIs(connect.return_value.__exit__.call_args.args[0], psycopg.Error)
        cursor.fetchall.assert_not_called()
        connect.assert_called_once()

    def test_duplicate_table_race_is_rejected_without_overwrite(self):
        with patch("transitmind.agency_import.psycopg.connect") as connect:
            _, cursor = self.prepare_connection(connect)
            cursor.execute.side_effect = [None, None, psycopg.errors.DuplicateTable("private diagnostic")]
            with self.assertRaisesRegex(AgencyImportError, "already exists"):
                import_agencies(self.settings, self.snapshot)
        cursor.executemany.assert_not_called()

    def test_connection_and_commit_errors_do_not_report_success_or_expose_password(self):
        for fail_on_commit in (False, True):
            with self.subTest(fail_on_commit=fail_on_commit):
                with patch("transitmind.agency_import.psycopg.connect") as connect:
                    self.prepare_connection(connect)
                    error = psycopg.OperationalError(f"private connection diagnostic {FAKE_PASSWORD}")
                    if fail_on_commit:
                        connect.return_value.__exit__.side_effect = error
                    else:
                        connect.side_effect = error
                    with self.assertRaisesRegex(AgencyImportError, "could not be confirmed") as raised:
                        import_agencies(self.settings, self.snapshot)
                self.assertNotIn(FAKE_PASSWORD, str(raised.exception))
                connect.assert_called_once()


class AgencyCliTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = AgencySnapshot(Path("agency.txt"), (DEMO_AGENCY,), "test-hash")
        self.settings = DatabaseSettings("localhost", 5432, "transitmind", "postgres", FAKE_PASSWORD)

    def test_default_preview_does_not_load_credentials_or_connect(self):
        output = io.StringIO()
        with patch("transitmind.agency_import.read_agencies", return_value=self.snapshot) as read:
            with patch("transitmind.agency_import.load_database_settings") as load_settings:
                with patch("transitmind.agency_import.psycopg.connect") as connect:
                    with redirect_stdout(output):
                        code = main(["--source", "demo-feed"])
        self.assertEqual(code, 0)
        read.assert_called_once_with(Path("demo-feed"))
        load_settings.assert_not_called()
        connect.assert_not_called()
        self.assertIn("agency_preview", output.getvalue())
        self.assertIn("Source rows: 1", output.getvalue())
        self.assertIn("No tables were created", output.getvalue())
        self.assertNotIn("Committed: yes", output.getvalue())

    def test_apply_uses_explicit_target_and_prints_verified_result(self):
        output = io.StringIO()
        with patch("transitmind.agency_import.read_agencies", return_value=self.snapshot):
            with patch("transitmind.agency_import.load_database_settings", return_value=self.settings):
                with patch("transitmind.agency_import.import_agencies", return_value=ImportResult("transitmind", 1)) as apply:
                    with redirect_stdout(output):
                        code = main(["--source", "demo-feed", "--apply"])
        self.assertEqual(code, 0)
        apply.assert_called_once_with(self.settings, self.snapshot)
        self.assertIn("Target: transitmind.public.agency", output.getvalue())
        self.assertIn("Verified rows: 1", output.getvalue())
        self.assertIn("Committed: yes", output.getvalue())
        self.assertNotIn(FAKE_PASSWORD, output.getvalue())

    def test_invalid_source_stops_before_loading_database_configuration(self):
        with patch("transitmind.agency_import.read_agencies", side_effect=AgencyImportError("Invalid source.")):
            with patch("transitmind.agency_import.load_database_settings") as load_settings:
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main(["--source", "missing", "--apply"])
        self.assertEqual(raised.exception.code, 2)
        load_settings.assert_not_called()

    def test_apply_failure_exits_without_success_output(self):
        for error in (AgencyImportError("Import could not be confirmed."), DatabaseCheckError("Missing password.")):
            with self.subTest(error=error):
                output, errors = io.StringIO(), io.StringIO()
                with patch("transitmind.agency_import.read_agencies", return_value=self.snapshot):
                    with patch("transitmind.agency_import.load_database_settings", return_value=self.settings):
                        with patch("transitmind.agency_import.import_agencies", side_effect=error):
                            with redirect_stdout(output), redirect_stderr(errors):
                                with self.assertRaises(SystemExit) as raised:
                                    main(["--source", "demo-feed", "--apply"])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(str(error), errors.getvalue())
                self.assertNotIn("Committed: yes", output.getvalue())

    def test_help_never_reads_files_or_credentials(self):
        with patch("transitmind.agency_import.read_agencies") as read:
            with patch("transitmind.agency_import.load_database_settings") as load_settings:
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        read.assert_not_called()
        load_settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
