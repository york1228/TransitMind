"""SQL policy regression checks; parsing and mocked database execution only."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

import sqlglot
from sqlglot import exp

from transitmind.database_config import CHECK_SQL, DatabaseSettings
from transitmind.postgres_schema import build_postgres_tools
from transitmind.run_sql import prepare_sql, run_sql
from transitmind.runtime import SQL_SYSTEM_PROMPT
from transitmind.sql_policy import (
    ALLOWED_FUNCTIONS, ALLOWED_JOINS, ALLOWED_TABLES, MAX_JOINS, MAX_ROWS,
    MAX_SQL_LENGTH, MAX_TABLES, MIN_SQL_LENGTH, MODEL_PREVIEW_ROWS,
    SQL_READ_ONLY_OPTIONS, SQL_SCHEMA, sql_scope_description,
)


SETTINGS = DatabaseSettings("localhost", 5432, "transitmind", "transitmind_reader", "fake-test-password")
THREE_TABLE_SQL = (
    "SELECT r.route_id, COUNT(DISTINCT st.stop_id) AS stop_count "
    "FROM routes r JOIN trips t ON r.route_id = t.route_id "
    "JOIN stop_times st ON t.trip_id = st.trip_id GROUP BY r.route_id"
)


# 正例：保护已经开放的函数、四种关系和三表链，不执行查询。
class SqlPolicyPositiveTests(unittest.TestCase):
    def test_single_table_queries_and_allowed_functions(self):
        for table in ("agency", "routes", "trips", "stops", "stop_times", "calendar", "calendar_dates"):
            with self.subTest(table=table):
                statement = sqlglot.parse_one(prepare_sql(f"SELECT * FROM {table}"), read="postgres")
                self.assertEqual(statement.find(exp.Table).name, table)
                self.assertEqual(statement.find(exp.Table).db, SQL_SCHEMA)
        for expression, table, node in (
            ("COUNT(*)", "agency", exp.Count),
            ("LOWER(agency_name)", "agency", exp.Lower),
            ("UPPER(agency_name)", "agency", exp.Upper),
            ("MIN(departure_time)", "stop_times", exp.Min),
            ("MAX(departure_time)", "stop_times", exp.Max),
        ):
            with self.subTest(expression=expression):
                statement = sqlglot.parse_one(prepare_sql(f"SELECT {expression} FROM {table}"), read="postgres")
                self.assertIsInstance(statement.find(node), node)

    def test_all_declared_two_table_relationships_in_both_orders(self):
        for left, right, column in (
            ("agency", "routes", "agency_id"),
            ("routes", "trips", "route_id"),
            ("trips", "stop_times", "trip_id"),
            ("stops", "stop_times", "stop_id"),
        ):
            for first, second in ((left, right), (right, left)):
                with self.subTest(first=first, second=second):
                    sql = f"SELECT * FROM {first} a INNER JOIN {second} b ON a.{column} = b.{column}"
                    statement = sqlglot.parse_one(prepare_sql(sql), read="postgres")
                    self.assertEqual(len(statement.args["joins"]), 1)
                    self.assertEqual({table.name for table in statement.find_all(exp.Table)}, {left, right})

    def test_three_table_inner_join_with_distinct_stop_count(self):
        statement = sqlglot.parse_one(prepare_sql(THREE_TABLE_SQL), read="postgres")
        self.assertEqual(len(statement.args["joins"]), 2)
        self.assertIsInstance(statement.find(exp.Count).this, exp.Distinct)
        self.assertEqual({table.name for table in statement.find_all(exp.Table)}, {"routes", "trips", "stop_times"})

    def test_three_table_left_and_left_outer_joins(self):
        for join_type in ("LEFT JOIN", "LEFT OUTER JOIN"):
            with self.subTest(join_type=join_type):
                sql = THREE_TABLE_SQL.replace(" JOIN ", f" {join_type} ")
                statement = sqlglot.parse_one(prepare_sql(sql), read="postgres")
                self.assertEqual(len(statement.args["joins"]), 2)
                for join in statement.args["joins"]:
                    self.assertEqual(join.side, "LEFT")
                    self.assertEqual(join.kind, "OUTER" if join_type == "LEFT OUTER JOIN" else "")

    def test_three_table_chain_to_stops(self):
        sql = (
            "SELECT t.route_id, COUNT(DISTINCT s.stop_id) AS stop_count "
            "FROM trips t JOIN stop_times st ON t.trip_id = st.trip_id "
            "JOIN stops s ON st.stop_id = s.stop_id GROUP BY t.route_id"
        )
        statement = sqlglot.parse_one(prepare_sql(sql), read="postgres")
        self.assertEqual(len(statement.args["joins"]), 2)
        self.assertIsInstance(statement.find(exp.Count).this, exp.Distinct)

    def test_original_max_group_by_failure_is_now_supported(self):
        # 来自已有失败案例的 SQL；通过校验不等于已经验证业务结果或“每天”的日期口径。
        sql = (
            "SELECT t.route_id, MAX(st.departure_time) AS latest_departure "
            "FROM public.trips t JOIN public.stop_times st ON t.trip_id = st.trip_id "
            "GROUP BY t.route_id ORDER BY latest_departure DESC NULLS LAST"
        )
        statement = sqlglot.parse_one(prepare_sql(sql), read="postgres")
        self.assertIsInstance(statement.find(exp.Max), exp.Max)
        self.assertIsInstance(statement.args["group"], exp.Group)
        self.assertEqual(statement.args["order"].expressions[0].this.name, "latest_departure")


# 反例：仍然拒绝越权对象、错误连接和未开放语法，不把数据库语义检查全部搬进校验器。
class SqlPolicyNegativeTests(unittest.TestCase):
    def test_unapproved_table_schema_and_catalog(self):
        for table in ("public.users", "private.agency", "other.public.agency"):
            with self.subTest(table=table), self.assertRaisesRegex(ValueError, "configured public GTFS tables"):
                prepare_sql(f"SELECT * FROM {table}")

    def test_undeclared_relationships(self):
        for sql in (
            "SELECT * FROM trips t JOIN calendar c ON t.service_id = c.service_id",
            "SELECT * FROM agency a JOIN stops s ON a.agency_id = s.stop_id",
        ):
            with self.subTest(sql=sql), self.assertRaisesRegex(ValueError, "declared relationship"):
                prepare_sql(sql)

    def test_incorrect_or_unqualified_join_columns(self):
        for condition in (
            "r.route_id = t.trip_id",
            "r.agency_id = t.service_id",
            "r.route_id = '1'",
            "route_id = t.route_id",
        ):
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                prepare_sql(f"SELECT * FROM routes r JOIN trips t ON {condition}")

    def test_disallowed_join_types_and_missing_on(self):
        for sql in (
            "SELECT * FROM routes r CROSS JOIN trips t",
            "SELECT * FROM routes r RIGHT JOIN trips t ON r.route_id = t.route_id",
            "SELECT * FROM routes r FULL JOIN trips t ON r.route_id = t.route_id",
            "SELECT * FROM routes r NATURAL JOIN trips t",
            "SELECT * FROM routes r JOIN trips t USING (route_id)",
            "SELECT * FROM routes r JOIN trips t",
            "SELECT * FROM routes r, trips t WHERE r.route_id = t.route_id",
            "SELECT * FROM routes r JOIN trips t ON r.route_id = t.route_id AND t.trip_id = '1'",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                prepare_sql(sql)

    def test_self_joins_and_duplicate_aliases(self):
        for sql in (
            "SELECT * FROM routes r JOIN routes other_route ON r.route_id = other_route.route_id",
            "SELECT * FROM routes x JOIN trips x ON x.route_id = x.route_id",
        ):
            with self.subTest(sql=sql), self.assertRaisesRegex(ValueError, "unique table aliases; self joins"):
                prepare_sql(sql)

    def test_each_join_connects_new_table_to_an_earlier_table(self):
        for sql in (
            "SELECT * FROM routes r JOIN trips t ON t.trip_id = st.trip_id "
            "JOIN stop_times st ON r.route_id = t.route_id",
            "SELECT * FROM routes r JOIN trips t ON r.route_id = t.route_id "
            "JOIN stop_times st ON r.route_id = t.route_id",
        ):
            with self.subTest(sql=sql), self.assertRaisesRegex(ValueError, "added table to an earlier table"):
                prepare_sql(sql)

    def test_functions_and_expressions_outside_whitelist(self):
        for expression in (
            "SUM(stop_sequence)", "AVG(stop_sequence)", "ROUND(stop_sequence)",
            "CAST(stop_sequence AS TEXT)", "CASE WHEN stop_sequence = 1 THEN 1 ELSE 0 END",
        ):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                prepare_sql(f"SELECT {expression} FROM stop_times")

    def test_subqueries_and_with_are_rejected(self):
        for sql in (
            "SELECT * FROM (SELECT * FROM routes) r",
            "SELECT * FROM routes WHERE route_id IN (SELECT route_id FROM trips)",
            "WITH route_list AS (SELECT * FROM routes) SELECT * FROM route_list",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                prepare_sql(sql)

    def test_writes_and_multiple_statements_are_rejected(self):
        for sql in (
            "UPDATE stops SET stop_name = 'changed'",
            "DELETE FROM stops",
            "INSERT INTO stops (stop_id) VALUES ('1')",
            "DROP TABLE stops",
            "SELECT agency_id INTO agency_copy FROM agency",
            "SELECT * FROM agency; SELECT * FROM routes",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                prepare_sql(sql)


# 边界：数量诊断、LIMIT、结果分流，以及执行规则与模型说明的一致性。
class SqlPolicyBoundaryTests(unittest.TestCase):
    def test_zero_and_four_tables_report_table_count_not_permissions(self):
        four_table_sql = (
            "SELECT * FROM agency a JOIN routes r ON a.agency_id = r.agency_id "
            "JOIN trips t ON r.route_id = t.route_id JOIN stop_times st ON t.trip_id = st.trip_id"
        )
        for sql in ("SELECT 1", four_table_sql):
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError) as error:
                    prepare_sql(sql)
                self.assertEqual(str(error.exception), f"Use between 1 and {MAX_TABLES} tables.")

    def test_small_limits_and_public_schema_are_preserved_for_three_tables(self):
        for limit in (0, 5, MAX_ROWS):
            with self.subTest(limit=limit):
                statement = sqlglot.parse_one(prepare_sql(f"{THREE_TABLE_SQL} LIMIT {limit}"), read="postgres")
                self.assertEqual(int(statement.args["limit"].expression.this), limit)
                self.assertEqual(len(statement.args["joins"]), 2)
                self.assertTrue(all(table.db == SQL_SCHEMA for table in statement.find_all(exp.Table)))

    def test_default_and_large_limits_keep_one_extra_row(self):
        for suffix in ("", f" LIMIT {MAX_ROWS + 1}", " LIMIT 1000"):
            with self.subTest(suffix=suffix):
                statement = sqlglot.parse_one(prepare_sql("SELECT route_id FROM routes" + suffix), read="postgres")
                self.assertEqual(int(statement.args["limit"].expression.this), MAX_ROWS + 1)

    def test_non_integer_and_negative_limits_are_rejected(self):
        for limit in ("-1", "'5'", "1.5", "route_id", "1 + 1", "NULL"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                prepare_sql(f"SELECT route_id FROM routes LIMIT {limit}")

    def test_original_sql_execution_metadata_and_result_split(self):
        original_sql = "select route_id from routes limit 1000"
        expected_sql = f"SELECT route_id FROM public.routes LIMIT {MAX_ROWS + 1}"
        # 全部是模拟行和模拟连接，用边界数量检查 100 行保留、12 行预览及额外一行。
        for row_count in (0, 5, MODEL_PREVIEW_ROWS, MODEL_PREVIEW_ROWS + 1, MAX_ROWS, MAX_ROWS + 1):
            with self.subTest(row_count=row_count), patch("transitmind.run_sql.psycopg.connect") as connect:
                cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
                cursor.fetchone.return_value = ("transitmind", "transitmind_reader", "on")
                cursor.description = [SimpleNamespace(name="route_id")]
                rows = [(str(index),) for index in range(row_count)]
                cursor.fetchmany.return_value = rows

                result = run_sql(SETTINGS, {"sql": original_sql})

                self.assertTrue(result.success, result.content)
                self.assertEqual(result.metadata["original_sql"], original_sql)
                self.assertEqual(result.metadata["prepared_sql"], expected_sql)
                self.assertEqual(result.metadata["executed_sql"], expected_sql)
                self.assertEqual(cursor.execute.call_args_list, [call(CHECK_SQL), call(expected_sql)])
                cursor.fetchmany.assert_called_once_with(MAX_ROWS + 1)
                self.assertEqual(connect.call_args.kwargs["options"], SQL_READ_ONLY_OPTIONS)
                self.assertIn("default_transaction_read_only=on", connect.call_args.kwargs["options"])
                connect.return_value.__exit__.assert_called_once_with(None, None, None)

                retained_rows = [list(row) for row in rows[:MAX_ROWS]]
                preview_rows = retained_rows[:MODEL_PREVIEW_ROWS]
                payload = json.loads(result.content)
                self.assertEqual(result.metadata["rows"], retained_rows)
                self.assertEqual(payload["rows"], preview_rows)
                self.assertEqual(result.metadata["row_count"], len(retained_rows))
                self.assertEqual(payload["row_count"], len(retained_rows))
                self.assertEqual(result.metadata["preview_row_count"], len(preview_rows))
                self.assertEqual(payload["displayed_row_count"], len(preview_rows))
                self.assertEqual(payload["sql"], expected_sql)
                self.assertEqual(payload["row_limit"], MAX_ROWS)
                for data in (result.metadata, payload):
                    self.assertEqual(data["truncated"], row_count > MAX_ROWS)
                    self.assertEqual(data["preview_truncated"], len(retained_rows) > len(preview_rows))

    def test_declared_policy_tool_description_and_system_prompt_agree(self):
        self.assertEqual(ALLOWED_TABLES, {"agency", "routes", "trips", "stops", "stop_times", "calendar", "calendar_dates"})
        self.assertEqual(ALLOWED_JOINS, {
            ("agency", "routes"): "agency_id",
            ("routes", "trips"): "route_id",
            ("trips", "stop_times"): "trip_id",
            ("stops", "stop_times"): "stop_id",
        })
        self.assertEqual(set(ALLOWED_FUNCTIONS), {"COUNT", "LOWER", "UPPER", "MIN", "MAX"})
        self.assertEqual((MAX_TABLES, MAX_JOINS, MAX_ROWS, MODEL_PREVIEW_ROWS), (3, 2, 100, 12))
        self.assertEqual(MAX_JOINS, MAX_TABLES - 1)
        scope = sql_scope_description()
        self.assertIn(scope, SQL_SYSTEM_PROMPT)
        for (left, right), column in ALLOWED_JOINS.items():
            self.assertIn(f"{left}.{column} = {right}.{column}", scope)
        for name in ALLOWED_FUNCTIONS:
            self.assertIn(name, scope)
        with patch("transitmind.postgres_schema.psycopg.connect") as connect:
            registry = build_postgres_tools(SETTINGS, include_sql=True)
            connect.assert_not_called()
        definition = next(item for item in registry.definitions() if item.name == "run_sql")
        self.assertIn(scope, definition.description)
        self.assertEqual(definition.parameters["properties"]["sql"], {
            "type": "string", "minLength": MIN_SQL_LENGTH, "maxLength": MAX_SQL_LENGTH,
        })


if __name__ == "__main__":
    unittest.main()
