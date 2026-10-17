import contextlib
import io
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import psycopg

from transitmind import postgres_check
from transitmind.postgres_check import (
    CHECK_SQL,
    READ_ONLY_OPTIONS,
    ConnectionCheck,
    DatabaseCheckError,
    DatabaseSettings,
    check_connection,
    load_database_settings,
    main,
)


FAKE_PASSWORD = "fake-pg-password-not-real"


class DatabaseSettingsTests(unittest.TestCase):
    def settings_from_environment(self, overrides=None):
        environment = {"TRANSITMIND_DB_PASSWORD": FAKE_PASSWORD, **(overrides or {})}
        with patch.dict(os.environ, environment, clear=True):
            with patch("transitmind.database_config.load_dotenv") as load_env:
                settings = load_database_settings()
        return settings, load_env

    def test_defaults_and_project_local_environment_file(self):
        settings, load_env = self.settings_from_environment()
        self.assertEqual((settings.host, settings.port, settings.database, settings.user),
                         ("localhost", 5432, "transitmind", "postgres"))
        self.assertEqual(settings.password, FAKE_PASSWORD)
        expected_path = Path(postgres_check.__file__).resolve().parent.parent / ".env"
        load_env.assert_called_once_with(expected_path, override=False)

    def test_local_host_port_and_user_can_be_configured(self):
        for host in ("localhost", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                settings, _ = self.settings_from_environment({
                    "TRANSITMIND_DB_HOST": host,
                    "TRANSITMIND_DB_PORT": "15432",
                    "TRANSITMIND_DB_USER": "local_test_user",
                })
                self.assertEqual((settings.host, settings.port, settings.user),
                                 (host, 15432, "local_test_user"))

    def test_password_is_not_trimmed_or_included_in_repr(self):
        settings, _ = self.settings_from_environment({"TRANSITMIND_DB_PASSWORD": " leading and trailing "})
        self.assertEqual(settings.password, " leading and trailing ")
        self.assertNotIn(settings.password, repr(settings))
        self.assertNotIn("password=", repr(settings))

    def test_query_credentials_are_separate_from_import_credentials(self):
        query_password = " fake-query-password "
        with patch.dict(os.environ, {
            "TRANSITMIND_DB_PASSWORD": FAKE_PASSWORD,
            "TRANSITMIND_QUERY_DB_USER": "transitmind_reader",
            "TRANSITMIND_QUERY_DB_PASSWORD": query_password,
        }, clear=True), patch("transitmind.database_config.load_dotenv"):
            admin = load_database_settings()
            query = load_database_settings(for_query=True)
        self.assertEqual((admin.user, admin.password), ("postgres", FAKE_PASSWORD))
        self.assertEqual((query.user, query.password), ("transitmind_reader", query_password))
        self.assertEqual((query.host, query.port, query.database), (admin.host, admin.port, admin.database))
        self.assertNotIn(query_password, repr(query))

    def test_query_configuration_never_falls_back_to_admin(self):
        for override in ({}, {"TRANSITMIND_QUERY_DB_PASSWORD": ""},
                         {"TRANSITMIND_QUERY_DB_USER": "postgres", "TRANSITMIND_QUERY_DB_PASSWORD": FAKE_PASSWORD}):
            with self.subTest(override=override), patch.dict(os.environ, {
                "TRANSITMIND_DB_PASSWORD": FAKE_PASSWORD, **override,
            }, clear=True), patch("transitmind.database_config.load_dotenv"):
                with self.assertRaises(DatabaseCheckError) as raised:
                    load_database_settings(for_query=True)
                self.assertIn("TRANSITMIND_QUERY_DB", str(raised.exception))
                self.assertNotIn(FAKE_PASSWORD, str(raised.exception))

    def test_invalid_configuration_is_rejected(self):
        overrides = [
            {"TRANSITMIND_DB_PORT": "not-a-port"},
            {"TRANSITMIND_DB_PORT": ""},
            {"TRANSITMIND_DB_PORT": "0"},
            {"TRANSITMIND_DB_PORT": "65536"},
            {"TRANSITMIND_DB_HOST": "example.com"},
            {"TRANSITMIND_DB_NAME": "postgres"},
            {"TRANSITMIND_DB_USER": " "},
            {"TRANSITMIND_DB_PASSWORD": ""},
        ]
        for override in overrides:
            with self.subTest(override=override):
                with self.assertRaises(DatabaseCheckError) as raised:
                    self.settings_from_environment(override)
                self.assertNotIn(FAKE_PASSWORD, str(raised.exception))

    def test_local_environment_file_errors_are_redacted(self):
        for error_type in (OSError, ValueError):
            with self.subTest(error_type=error_type):
                with patch("transitmind.database_config.load_dotenv",
                           side_effect=error_type(f"private configuration diagnostic {FAKE_PASSWORD}")):
                    with self.assertRaises(DatabaseCheckError) as raised:
                        load_database_settings()
                self.assertNotIn(FAKE_PASSWORD, str(raised.exception))
                self.assertNotIn("private configuration diagnostic", str(raised.exception))


class PostgresCheckTests(unittest.TestCase):
    def setUp(self):
        self.settings = DatabaseSettings("localhost", 5432, "transitmind", "postgres", FAKE_PASSWORD)

    def test_fixed_read_only_query_and_connection_bounds(self):
        with patch("transitmind.postgres_check.psycopg.connect") as connect:
            connection = connect.return_value.__enter__.return_value
            cursor = connection.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = ("transitmind", "postgres", "on")
            result = check_connection(self.settings)

        self.assertEqual(result, ConnectionCheck("transitmind", "postgres", True))
        connect.assert_called_once_with(
            host="localhost", port=5432, dbname="transitmind", user="postgres", password=FAKE_PASSWORD,
            connect_timeout=5, autocommit=True, options=READ_ONLY_OPTIONS,
            application_name="transitmind_connection_check",
        )
        cursor.execute.assert_called_once_with(CHECK_SQL)
        cursor.fetchone.assert_called_once_with()
        self.assertTrue(CHECK_SQL.startswith("SELECT "))
        self.assertIn("default_transaction_read_only=on", READ_ONLY_OPTIONS)
        self.assertIn("statement_timeout=5000", READ_ONLY_OPTIONS)
        connection.cursor.return_value.__exit__.assert_called_once()
        connect.return_value.__exit__.assert_called_once()

    def test_connection_error_is_redacted_and_not_retried(self):
        with patch("transitmind.postgres_check.psycopg.connect",
                   side_effect=psycopg.OperationalError(f"private diagnostic {FAKE_PASSWORD}")) as connect:
            with self.assertRaises(DatabaseCheckError) as raised:
                check_connection(self.settings)
        self.assertNotIn(FAKE_PASSWORD, str(raised.exception))
        self.assertNotIn("private diagnostic", str(raised.exception))
        connect.assert_called_once()

    def test_query_error_is_redacted_and_connection_is_closed(self):
        with patch("transitmind.postgres_check.psycopg.connect") as connect:
            connection = connect.return_value.__enter__.return_value
            cursor = connection.cursor.return_value.__enter__.return_value
            cursor.execute.side_effect = psycopg.Error(f"private query diagnostic {FAKE_PASSWORD}")
            with self.assertRaises(DatabaseCheckError) as raised:
                check_connection(self.settings)
        self.assertNotIn(FAKE_PASSWORD, str(raised.exception))
        connect.return_value.__exit__.assert_called_once()

    def test_unexpected_result_database_or_read_only_mode_is_rejected(self):
        for row in (None, (), ("transitmind",), ("postgres", "postgres", "on"),
                    ("transitmind", "postgres", "off"), ("transitmind", "unexpected_user", "on")):
            with self.subTest(row=row):
                with patch("transitmind.postgres_check.psycopg.connect") as connect:
                    connection = connect.return_value.__enter__.return_value
                    cursor = connection.cursor.return_value.__enter__.return_value
                    cursor.fetchone.return_value = row
                    with self.assertRaises(DatabaseCheckError):
                        check_connection(self.settings)

    def test_successful_cli_prints_safe_connection_summary(self):
        for query_mode in (False, True):
            with self.subTest(query_mode=query_mode):
                output = io.StringIO()
                user = "transitmind_reader" if query_mode else "postgres"
                settings = replace(self.settings, user=user)
                with patch("transitmind.postgres_check.load_database_settings", return_value=settings) as load:
                    with patch("transitmind.postgres_check.check_connection",
                               return_value=ConnectionCheck("transitmind", user, True)) as check:
                        with contextlib.redirect_stdout(output):
                            code = main(["--query"] if query_mode else [])
                self.assertEqual(code, 0)
                load.assert_called_once_with(for_query=query_mode)
                check.assert_called_once_with(settings)
                self.assertIn("Database: transitmind", output.getvalue())
                self.assertIn(f"Account: {user}", output.getvalue())
                self.assertIn("Session read-only: on", output.getvalue())
                self.assertIn("no LLM", output.getvalue())
                self.assertNotIn(FAKE_PASSWORD, output.getvalue())

    def test_configuration_error_exits_before_connecting(self):
        errors = io.StringIO()
        with patch("transitmind.postgres_check.load_database_settings",
                   side_effect=DatabaseCheckError("Missing local database password.")):
            with patch("transitmind.postgres_check.psycopg.connect") as connect:
                with contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as raised:
                        main([])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Missing local database password", errors.getvalue())
        connect.assert_not_called()

    def test_database_error_has_no_raw_exception_in_cli_output(self):
        errors = io.StringIO()
        with patch("transitmind.postgres_check.load_database_settings", return_value=self.settings):
            with patch("transitmind.postgres_check.psycopg.connect",
                       side_effect=psycopg.OperationalError(f"private diagnostic {FAKE_PASSWORD}")):
                with contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as raised:
                        main([])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn(FAKE_PASSWORD, errors.getvalue())
        self.assertNotIn("private diagnostic", errors.getvalue())

    def test_help_does_not_load_credentials_or_connect(self):
        with patch("transitmind.postgres_check.load_database_settings") as load_settings:
            with patch("transitmind.postgres_check.psycopg.connect") as connect:
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        load_settings.assert_not_called()
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
