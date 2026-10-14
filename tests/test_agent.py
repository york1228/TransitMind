import json
import unittest
from collections.abc import Sequence

from transitmind.agent import Agent
from transitmind.demo import ScriptedModel, build_demo_tools, describe_table
from transitmind.models import Message, ToolCall, ToolDefinition, ToolResult
from transitmind.tools import Tool, ToolRegistry


class SequenceModel:
    def __init__(self, responses: list[Message]) -> None:
        self.responses = iter(responses)
        self.requests: list[tuple[Message, ...]] = []
        self.tool_definitions: list[tuple[ToolDefinition, ...]] = []

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolDefinition]) -> Message:
        self.requests.append(tuple(messages))
        self.tool_definitions.append(tuple(tools))
        return next(self.responses)


class AgentTests(unittest.TestCase):
    def test_tool_result_reaches_the_next_model_request_with_its_call_id(self) -> None:
        model = SequenceModel([
            Message("assistant", tool_calls=(ToolCall("c1", "describe_table", {"table": "trips"}),)),
            Message("assistant", "Here are the fields."),
        ])
        result = Agent(model, build_demo_tools()).run("Describe trips.")

        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.final_answer, "Here are the fields.")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.tool_definitions[0][0].name, "describe_table")
        second_request = model.requests[1]
        self.assertEqual([message.role for message in second_request], ["system", "user", "assistant", "tool"])
        self.assertEqual(second_request[-1].tool_call_id, "c1")
        payload = json.loads(second_request[-1].content)
        self.assertTrue(payload["success"])
        self.assertIn("trip_id", json.loads(payload["content"])["columns"])

    def test_normal_answer_finishes_without_a_tool(self) -> None:
        model = SequenceModel([Message("assistant", "Hello.")])
        result = Agent(model, build_demo_tools()).run("Hello.")
        self.assertEqual(result.final_answer, "Hello.")
        self.assertFalse(any(event.kind == "tool_request" for event in result.events))

    def test_multiple_tool_calls_return_individually_matched_results(self) -> None:
        model = SequenceModel([
            Message("assistant", tool_calls=(
                ToolCall("c1", "describe_table", {"table": "trips"}),
                ToolCall("c2", "describe_table", {"table": "routes"}),
            )),
            Message("assistant", "Both tables were described."),
        ])
        result = Agent(model, build_demo_tools()).run("Describe both tables.")
        tool_messages = [message for message in result.messages if message.role == "tool"]
        self.assertEqual([message.tool_call_id for message in tool_messages], ["c1", "c2"])
        self.assertTrue(all(json.loads(message.content)["success"] for message in tool_messages))

    def test_failed_tool_result_is_returned_to_the_model(self) -> None:
        model = SequenceModel([
            Message("assistant", tool_calls=(ToolCall("c1", "missing_tool", {}),)),
            Message("assistant", "The tool is unavailable."),
        ])
        result = Agent(model, build_demo_tools()).run("Use a missing tool.")
        payload = json.loads(model.requests[1][-1].content)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "unknown_tool")
        event = next(event for event in result.events if event.kind == "tool_result")
        self.assertFalse(event.details["success"])

    def test_limit_does_not_invent_a_final_answer(self) -> None:
        model = SequenceModel([
            Message("assistant", tool_calls=(ToolCall("c1", "describe_table", {"table": "trips"}),)),
        ])
        result = Agent(model, build_demo_tools(), max_model_turns=1).run("Describe trips.")
        self.assertEqual(result.stop_reason, "model_turn_limit")
        self.assertIsNone(result.final_answer)
        self.assertEqual(result.messages[-1].role, "tool")

    def test_duplicate_call_ids_are_rejected_before_execution(self) -> None:
        executions = []
        registry = ToolRegistry()
        registry.register(Tool(
            ToolDefinition("count", "Count executions.", {}),
            lambda arguments: executions.append(arguments) or ToolResult(True, "counted"),
        ))
        model = SequenceModel([Message("assistant", tool_calls=(
            ToolCall("same", "count", {}), ToolCall("same", "count", {}),
        ))])
        with self.assertRaisesRegex(ValueError, "unique"):
            Agent(model, registry).run("Count.")
        self.assertEqual(executions, [])

    def test_call_ids_cannot_be_reused_in_a_later_turn(self) -> None:
        response = Message("assistant", tool_calls=(ToolCall("same", "describe_table", {"table": "trips"}),))
        with self.assertRaisesRegex(ValueError, "unique"):
            Agent(SequenceModel([response, response]), build_demo_tools()).run("Describe trips.")

    def test_invalid_model_role_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "assistant message"):
            Agent(SequenceModel([Message("user", "wrong role")]), build_demo_tools()).run("Hello.")

    def test_empty_model_response_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "tool calls or an answer"):
            Agent(SequenceModel([Message("assistant", " ")]), build_demo_tools()).run("Hello.")

    def test_invalid_budgets_and_empty_questions_are_rejected(self) -> None:
        for budget in (0, -1, True, 1.5):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                Agent(ScriptedModel(), build_demo_tools(), max_model_turns=budget)
        with self.assertRaises(ValueError):
            Agent(ScriptedModel(), build_demo_tools()).run(" ")

    def test_the_demo_reports_an_unknown_table_as_a_tool_failure(self) -> None:
        result = Agent(ScriptedModel("missing"), build_demo_tools()).run("Describe a missing table.")
        self.assertIn("unknown_table", result.final_answer)
        self.assertEqual(result.stop_reason, "completed")


class ToolRegistryTests(unittest.TestCase):
    def test_duplicate_registration_is_rejected(self) -> None:
        registry = build_demo_tools()
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(Tool(registry.definitions()[0], describe_table))

    def test_argument_errors_are_structured(self) -> None:
        registry = build_demo_tools()
        for arguments in ({}, {"table": 1}, {"table": "trips", "extra": True}):
            with self.subTest(arguments=arguments):
                result = registry.execute(ToolCall("c1", "describe_table", arguments))
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, "invalid_arguments")

    def test_tool_exceptions_are_returned_without_raw_exception_details(self) -> None:
        def broken(arguments: dict) -> ToolResult:
            raise RuntimeError("private internal diagnostic")

        registry = ToolRegistry()
        registry.register(Tool(ToolDefinition("broken", "A failing tool.", {}), broken))
        result = registry.execute(ToolCall("c1", "broken", {}))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "tool_execution_error")
        self.assertNotIn("private internal", result.content)


if __name__ == "__main__":
    unittest.main()

