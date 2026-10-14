import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from transitmind.__main__ import LIVE_SYSTEM_PROMPT, main
from transitmind.agent import Agent
from transitmind.deepseek_model import DeepSeekModel, ModelCallError, build_deepseek_model, message_to_api
from transitmind.demo import ScriptedModel, build_demo_tools
from transitmind.models import Message


def completion(content=None, calls=None, finish_reason=None, **extra):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [{
            "index": 0,
            "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
            "message": {"role": "assistant", "content": content, "tool_calls": calls, **extra},
        }],
    }


def tool_call(call_id="c1", arguments='{"table":"stops"}', name="describe_table"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class DeepSeekModelTests(unittest.TestCase):
    def model_with_responses(self, responses):
        requests = []
        pending = iter(responses)

        def send(request):
            requests.append(json.loads(request.content))
            reply = next(pending)
            if isinstance(reply, httpx.Response):
                return reply
            if isinstance(reply, Exception):
                raise reply
            return httpx.Response(200, json=reply)

        client = OpenAI(
            api_key="test-key-not-real",
            base_url="https://api.deepseek.com",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(send)),
        )
        self.addCleanup(client.close)
        return DeepSeekModel(client), requests

    def test_sdk_round_trip_preserves_tool_definition_call_and_result(self):
        model, requests = self.model_with_responses([
            completion(calls=[tool_call()]),
            completion("The stops fields include stop_id and stop_name."),
        ])
        run = Agent(model, build_demo_tools(), system_prompt=LIVE_SYSTEM_PROMPT).run("Describe stops.")

        self.assertEqual(run.stop_reason, "completed")
        self.assertEqual(len(requests), 2)
        first = requests[0]
        self.assertEqual(first["model"], "deepseek-flash")
        self.assertEqual(first["thinking"], {"type": "disabled"})
        self.assertEqual(first["tool_choice"], "auto")
        self.assertEqual(first["max_tokens"], 512)
        self.assertFalse(first["stream"])
        self.assertEqual(first["messages"][0]["content"], LIVE_SYSTEM_PROMPT)
        function = first["tools"][0]["function"]
        self.assertEqual(function["name"], "describe_table")
        self.assertEqual(function["parameters"]["required"], ["table"])
        second_messages = requests[1]["messages"]
        self.assertEqual([item["role"] for item in second_messages], ["system", "user", "assistant", "tool"])
        call = second_messages[2]["tool_calls"][0]
        self.assertEqual(call["id"], "c1")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"table": "stops"})
        self.assertEqual(second_messages[3]["tool_call_id"], "c1")
        result = json.loads(second_messages[3]["content"])
        self.assertTrue(result["success"])
        self.assertIn("stop_name", json.loads(result["content"])["columns"])

    def test_plain_answer_needs_no_tool_and_only_one_request(self):
        model, requests = self.model_with_responses([completion("Hello.")])
        run = Agent(model, build_demo_tools()).run("Hello.")
        self.assertEqual(run.final_answer, "Hello.")
        self.assertEqual(len(requests), 1)
        self.assertFalse(any(event.kind == "tool_request" for event in run.events))

    def test_multiple_calls_keep_their_own_result_ids(self):
        model, requests = self.model_with_responses([
            completion(calls=[tool_call("c1"), tool_call("c2", '{"table":"trips"}')]),
            completion("Both tables were described."),
        ])
        Agent(model, build_demo_tools()).run("Describe two tables.")
        results = [item for item in requests[1]["messages"] if item["role"] == "tool"]
        self.assertEqual([item["tool_call_id"] for item in results], ["c1", "c2"])

    def test_unknown_tool_failure_is_returned_to_the_model(self):
        model, requests = self.model_with_responses([
            completion(calls=[tool_call(name="missing_tool")]),
            completion("The requested tool is unavailable."),
        ])
        Agent(model, build_demo_tools()).run("Use a tool.")
        result = json.loads(requests[1]["messages"][-1]["content"])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "unknown_tool")

    def test_budget_stops_without_a_second_api_request(self):
        model, requests = self.model_with_responses([completion(calls=[tool_call()])])
        run = Agent(model, build_demo_tools(), max_model_turns=1).run("Describe stops.")
        self.assertEqual(run.stop_reason, "model_turn_limit")
        self.assertIsNone(run.final_answer)
        self.assertEqual(len(requests), 1)

    def test_bad_json_or_non_object_arguments_are_rejected(self):
        for arguments in ('{"table":', "[]", "null", '"stops"'):
            with self.subTest(arguments=arguments):
                model, _ = self.model_with_responses([completion(calls=[tool_call(arguments=arguments)])])
                with self.assertRaises(ModelCallError):
                    Agent(model, build_demo_tools()).run("Describe stops.")

    def test_incomplete_responses_are_not_accepted_as_answers(self):
        for finish_reason in ("length", "content_filter", "insufficient_system_resource"):
            with self.subTest(finish_reason=finish_reason):
                model, _ = self.model_with_responses([completion("Partial answer.", finish_reason=finish_reason)])
                with self.assertRaisesRegex(ModelCallError, "did not finish"):
                    Agent(model, build_demo_tools()).run("Describe stops.")

    def test_empty_choices_are_rejected(self):
        response = completion("Hello.")
        response["choices"] = []
        model, _ = self.model_with_responses([response])
        with self.assertRaisesRegex(ModelCallError, "no response choices"):
            Agent(model, build_demo_tools()).run("Hello.")

    def test_unexpected_thinking_content_is_rejected(self):
        model, _ = self.model_with_responses([completion("Hello.", reasoning_content="Unexpected thinking.")])
        with self.assertRaisesRegex(ModelCallError, "non-thinking"):
            Agent(model, build_demo_tools()).run("Hello.")

    def test_empty_assistant_response_is_rejected_by_the_agent(self):
        model, _ = self.model_with_responses([completion()])
        with self.assertRaisesRegex(ValueError, "tool calls or an answer"):
            Agent(model, build_demo_tools()).run("Hello.")

    def test_no_tools_omits_tool_fields(self):
        model, requests = self.model_with_responses([completion("Hello.")])
        model.complete([Message("user", "Hello.")], [])
        self.assertNotIn("tools", requests[0])
        self.assertNotIn("tool_choice", requests[0])

    def test_api_errors_are_redacted_and_not_retried(self):
        for status in (401, 402, 429, 500):
            with self.subTest(status=status):
                response = httpx.Response(status, json={"error": {"message": "private diagnostic secret-test"}})
                model, requests = self.model_with_responses([response])
                with self.assertRaises(ModelCallError) as raised:
                    Agent(model, build_demo_tools()).run("Hello.")
                self.assertNotIn("secret-test", str(raised.exception))
                self.assertEqual(len(requests), 1)

    def test_network_errors_are_redacted_and_not_retried(self):
        for error in (httpx.ConnectError("private diagnostic"), httpx.ReadTimeout("private diagnostic")):
            with self.subTest(error=type(error).__name__):
                model, requests = self.model_with_responses([error])
                with self.assertRaises(ModelCallError) as raised:
                    Agent(model, build_demo_tools()).run("Hello.")
                self.assertNotIn("private diagnostic", str(raised.exception))
                self.assertEqual(len(requests), 1)

    def test_tool_result_without_call_id_is_rejected(self):
        with self.assertRaisesRegex(ModelCallError, "identify"):
            message_to_api(Message("tool", "result"))

    def test_environment_defaults_and_request_bounds(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key-not-real"}, clear=True):
            with patch("transitmind.deepseek_model.load_dotenv") as load_env:
                with patch("transitmind.deepseek_model.OpenAI") as client:
                    model = build_deepseek_model()
        self.assertEqual(model.model, "deepseek-flash")
        self.assertEqual(load_env.call_args.args[0].name, ".env")
        self.assertEqual(load_env.call_args.args[0].parent.name, "TransitMind")
        self.assertFalse(load_env.call_args.kwargs["override"])
        self.assertEqual(client.call_args.kwargs["base_url"], "https://api.deepseek.com")
        self.assertEqual(client.call_args.kwargs["max_retries"], 0)
        self.assertEqual(client.call_args.kwargs["timeout"], 30.0)

    def test_missing_key_or_unapproved_endpoint_is_rejected(self):
        environments = [
            {},
            {"DEEPSEEK_API_KEY": "test", "DEEPSEEK_BASE_URL": "http://api.deepseek.com"},
            {"DEEPSEEK_API_KEY": "test", "DEEPSEEK_BASE_URL": "https://example.com"},
            {"DEEPSEEK_API_KEY": "test", "DEEPSEEK_BASE_URL": "https://api.deepseek.com:not-a-port"},
            {"DEEPSEEK_API_KEY": "test", "DEEPSEEK_BASE_URL": "https://api.deepseek.com/?token=private"},
            {"DEEPSEEK_API_KEY": "test", "DEEPSEEK_MODEL": " "},
        ]
        for environment in environments:
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True):
                    with patch("transitmind.deepseek_model.load_dotenv"):
                        with self.assertRaises(ValueError):
                            build_deepseek_model()

    def test_custom_system_prompt_reaches_the_model(self):
        model, requests = self.model_with_responses([completion("Hello.")])
        Agent(model, build_demo_tools(), system_prompt="Custom instruction.").run("Hello.")
        self.assertEqual(requests[0]["messages"][0]["content"], "Custom instruction.")
        with self.assertRaisesRegex(ValueError, "system prompt"):
            Agent(ScriptedModel(), build_demo_tools(), system_prompt=" ")

    def test_live_cli_uses_free_text_and_reports_the_real_mode(self):
        model, requests = self.model_with_responses([completion("Hello.")])
        output = io.StringIO()
        with patch("transitmind.deepseek_model.build_deepseek_model", return_value=model):
            with patch("sys.argv", ["transitmind", "--live", "--question", "Hello.", "--json"]):
                with contextlib.redirect_stdout(output):
                    code = main()
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["mode"], "deepseek_live")
        self.assertEqual(requests[0]["messages"][1]["content"], "Hello.")
        self.assertEqual(payload["final_answer"], "Hello.")

    def test_scripted_cli_does_not_pretend_to_parse_free_text(self):
        with patch("sys.argv", ["transitmind", "--question", "Hello."]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main()
        self.assertEqual(raised.exception.code, 2)

    def test_live_cli_reports_a_missing_key_before_any_request(self):
        error_output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with patch("transitmind.deepseek_model.load_dotenv"):
                with patch("transitmind.deepseek_model.OpenAI") as client:
                    with patch("sys.argv", ["transitmind", "--live", "--question", "Hello."]):
                        with contextlib.redirect_stderr(error_output):
                            with self.assertRaises(SystemExit) as raised:
                                main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("DEEPSEEK_API_KEY", error_output.getvalue())
        client.assert_not_called()

    def test_live_cli_reports_api_failure_without_printing_an_answer_or_secret(self):
        model, _ = self.model_with_responses([
            httpx.Response(401, json={"error": {"message": "secret-test"}}),
        ])
        output, error_output = io.StringIO(), io.StringIO()
        with patch("transitmind.deepseek_model.build_deepseek_model", return_value=model):
            with patch("sys.argv", ["transitmind", "--live", "--question", "Hello."]):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error_output):
                    with self.assertRaises(SystemExit) as raised:
                        main()
        self.assertEqual(raised.exception.code, 1)
        self.assertNotIn("Answer:", output.getvalue())
        self.assertNotIn("secret-test", error_output.getvalue())
        self.assertIn("authentication failed", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
