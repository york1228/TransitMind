import io
import json
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import call, patch

import psycopg

from transitmind.__main__ import DATABASE_SYSTEM_PROMPT, main
from transitmind.agent import Agent
from transitmind.demo import ScriptedModel
from transitmind.postgres_check import CHECK_SQL, READ_ONLY_OPTIONS, DatabaseSettings
from transitmind.postgres_schema import COLUMNS_SQL, PRIMARY_KEY_SQL, FOREIGN_KEYS_SQL, build_postgres_tools, describe_table


SETTINGS = DatabaseSettings("localhost", 5432, "transitmind", "transitmind_reader", "fake-test-password")
COLUMNS = [("agency_id", "text", "NO"), ("agency_name", "text", "YES")]


def prepare_connection(connect):
    cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("transitmind", "transitmind_reader", "on")
    cursor.fetchall.side_effect = [COLUMNS, [("agency_id",)], []]
    return cursor


class PostgresSchemaTests(unittest.TestCase):
    def test_read_only_bound_parameters_and_result_flow(self):
        table = "agency'; DROP TABLE agency; --"
        with patch("transitmind.postgres_schema.psycopg.connect") as connect:
            cursor = prepare_connection(connect)
            result = Agent(ScriptedModel(table), build_postgres_tools(SETTINGS)).run("Describe the table.")
        self.assertEqual(cursor.execute.call_args_list, [
            call(CHECK_SQL), call(COLUMNS_SQL, ("public", table)), call(PRIMARY_KEY_SQL, ("public", table)),
            call(FOREIGN_KEYS_SQL, ("public", table)),
        ])
        self.assertNotIn(table, COLUMNS_SQL + PRIMARY_KEY_SQL + FOREIGN_KEYS_SQL)
        self.assertEqual(connect.call_args.kwargs["options"], READ_ONLY_OPTIONS)
        self.assertTrue(connect.call_args.kwargs["autocommit"])
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 5)
        connect.return_value.__exit__.assert_called_once_with(None, None, None)
        payload = json.loads(json.loads(result.messages[3].content)["content"])
        self.assertEqual(payload["source"], "postgresql")
        self.assertEqual(payload["primary_key"], ["agency_id"])
        self.assertEqual(payload["foreign_keys"], [])
        self.assertEqual([column["nullable"] for column in payload["column_details"]], [False, True])
        self.assertEqual(result.messages[3].tool_call_id, "demo-call-1")
        self.assertIn("database fields: agency_id, agency_name", result.final_answer)
        self.assertEqual(result.stop_reason, "completed")

    def test_argument_target_session_and_database_failures(self):
        for arguments in ({}, {"table": 1}, {"table": " "}, {"table": "agency", "extra": True}):
            with self.subTest(arguments=arguments), patch("transitmind.postgres_schema.psycopg.connect") as connect:
                self.assertEqual(describe_table(SETTINGS, arguments).error_code, "invalid_arguments")
                connect.assert_not_called()
        for settings in (replace(SETTINGS, database="postgres"), replace(SETTINGS, host="example.org")):
            with self.subTest(settings=settings), patch("transitmind.postgres_schema.psycopg.connect") as connect:
                self.assertEqual(describe_table(settings, {"table": "agency"}).error_code, "invalid_database")
                connect.assert_not_called()
        for check, code in ((None, "invalid_database"), (("postgres", "transitmind_reader", "on"), "invalid_database"),
                            (("transitmind", "postgres", "on"), "invalid_user"),
                            (("transitmind", "transitmind_reader", "off"), "read_only_required")):
            with self.subTest(check=check), patch("transitmind.postgres_schema.psycopg.connect") as connect:
                cursor = prepare_connection(connect)
                cursor.fetchone.return_value = check
                self.assertEqual(describe_table(SETTINGS, {"table": "agency"}).error_code, code)
                cursor.execute.assert_called_once_with(CHECK_SQL)
        with patch("transitmind.postgres_schema.psycopg.connect") as connect:
            cursor = prepare_connection(connect)
            cursor.fetchall.side_effect = [[]]
            self.assertEqual(describe_table(SETTINGS, {"table": "missing"}).error_code, "unknown_table")
            self.assertEqual(cursor.execute.call_count, 2)
        with patch("transitmind.postgres_schema.psycopg.connect") as connect:
            cursor = prepare_connection(connect)
            cursor.execute.side_effect = psycopg.OperationalError("private diagnostic fake-test-password")
            result = describe_table(SETTINGS, {"table": "agency"})
            self.assertFalse(result.success)
            self.assertEqual(result.error_code, "schema_query_error")
            self.assertNotIn("private diagnostic", result.content)
            self.assertNotIn(SETTINGS.password, result.content)
            connect.assert_called_once()

    def test_cli_model_and_backend_switches_are_independent(self):
        for database in (False, True):
            for live in (False, True):
                with self.subTest(database=database, live=live):
                    output = io.StringIO()
                    table = "agency" if database else "trips"
                    argv = ["transitmind", "--json"] + (["--database"] if database else []) + (["--live"] if live else [])
                    with patch("transitmind.postgres_schema.psycopg.connect") as connect, \
                         patch("transitmind.database_config.load_database_settings", return_value=SETTINGS) as load, \
                         patch("transitmind.deepseek_model.build_deepseek_model", return_value=ScriptedModel(table)) as model, \
                         patch("sys.argv", argv), redirect_stdout(output):
                        prepare_connection(connect)
                        self.assertEqual(main(), 0)
                    payload = json.loads(output.getvalue())
                    mode = "deepseek_live" if live else "scripted_model"
                    self.assertEqual(payload["mode"], mode + ("_postgres_schema" if database else ""))
                    if database:
                        load.assert_called_once_with(for_query=True)
                        connect.assert_called_once()
                        self.assertEqual(payload["messages"][0]["content"], DATABASE_SYSTEM_PROMPT)
                    else:
                        load.assert_not_called()
                        connect.assert_not_called()
                    self.assertEqual(model.call_count, int(live))


if __name__ == "__main__":
    unittest.main()
