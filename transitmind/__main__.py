"""Run the scripted lesson or the real DeepSeek model from the command line."""

import argparse
import json
from pathlib import Path

from .agent import DEFAULT_MAX_SQL_ATTEMPTS, DEFAULT_MAX_TOOL_CALLS
from .application import execute_query
# 保留旧入口的提示词导出；查询运行逻辑已移到应用层。
from .runtime import (
    CONVERSATION_SYSTEM_PROMPT, DATABASE_SYSTEM_PROMPT, EXPERIENCE_SYSTEM_PROMPT,
    LIVE_SYSTEM_PROMPT,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="TransitMind: a scripted or real-model Agent loop.")
    parser.add_argument("--table", help="Table for schema-only demos (default: trips; agency with --database).")
    parser.add_argument("--live", action="store_true", help="Use the real DeepSeek API; API charges may apply.")
    parser.add_argument("--database", action="store_true", help="Read real PostgreSQL metadata; no generated SQL.")
    parser.add_argument("--sql", action="store_true", help="Enable read-only GTFS queries (requires --database).")
    parser.add_argument("--question", help="A free-text question for the real model (requires --live).")
    parser.add_argument("--max-model-turns", type=int, default=4)
    parser.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS, help="Total tool attempts per query; failures count (0 disables tools).")
    parser.add_argument("--max-sql-attempts", type=int, default=DEFAULT_MAX_SQL_ATTEMPTS, help="run_sql attempts per query; failures count (0 disables SQL).")
    parser.add_argument("--json", action="store_true", help="Print messages and events as JSON.")
    parser.add_argument("--trace-file", help="Save this run's messages and events to a JSON file.")
    parser.add_argument("--conversation-id", help="Load and save a local conversation with this ID (requires --live).")
    parser.add_argument("--history-turns", type=int, default=3, help="Previous user turns to include (default: 3; 0 sends no history).")
    parser.add_argument("--experience-memory", action="store_true", help="Retrieve approved query references before a live database SQL request.")
    parser.add_argument("--schema-memory", action="store_true", help="Enable schema_retrieve using a saved schema snapshot (requires --live --database --sql).")
    parser.add_argument("--schema-retrieval", choices=("keyword", "vector", "graph", "hybrid"), default="keyword", help="Schema retrieval method (default: keyword; hybrid requires both the vector index and Neo4j sync).")
    parser.add_argument("--schema-expand", action="store_true", help="Enable schema_expand for known table names (requires --schema-memory and a synced Neo4j graph).")
    args = parser.parse_args()
    if args.max_model_turns < 1:
        parser.error("--max-model-turns must be positive")
    if args.max_tool_calls < 0 or args.max_sql_attempts < 0:
        parser.error("--max-tool-calls and --max-sql-attempts must not be negative")
    if args.question is not None and not args.live:
        parser.error("--question requires --live; the scripted model does not interpret free text")
    if args.conversation_id is not None and not args.live:
        parser.error("--conversation-id requires --live; scripted demos do not interpret follow-up questions")
    if args.history_turns < 0:
        parser.error("--history-turns must not be negative")
    if args.sql and not args.database:
        parser.error("--sql requires --database")
    if args.sql and args.table not in {None, "agency"}:
        parser.error("--table selects schema-only demos; for SQL questions use --live --question instead")
    if args.experience_memory and not (args.live and args.database and args.sql):
        parser.error("--experience-memory requires --live --database --sql")
    if args.schema_memory and not (args.live and args.database and args.sql):
        parser.error("--schema-memory requires --live --database --sql")
    if args.schema_retrieval != "keyword" and not args.schema_memory:
        parser.error(f"--schema-retrieval {args.schema_retrieval} requires --schema-memory")
    if args.schema_expand and not args.schema_memory:
        parser.error("--schema-expand requires --schema-memory")

    table = args.table if args.table is not None else ("agency" if args.database else "trips")
    default_question = "List the transport agency IDs and names." if args.sql else f"Which fields belong to the {table} table?"
    question = args.question if args.question is not None else default_question

    def print_query_header(payload: dict, detail: str) -> None:
        # 输出仍归 CLI；应用层在准备好上下文后、请求模型之前调用这里。
        print(f"Mode: {payload['mode']} ({detail})", flush=True)
        if "conversation_id" in payload:
            print(f"Conversation: {payload['conversation_id']} | History messages: {payload['history_message_count']}", flush=True)
        if args.experience_memory:
            matches = payload["experience_memory"]["matches"]
            ids = ", ".join(item["id"] for item in matches) or "none"
            print(f"Experience matches: {len(matches)} | IDs: {ids}", flush=True)
        if args.schema_memory:
            print(f"Schema memory: enabled ({args.schema_retrieval}; schema_retrieve tool)", flush=True)
        if args.schema_expand:
            print("Schema expansion: enabled (schema_expand; one-hop Neo4j neighbors)", flush=True)
        print(f"Question: {payload['question']}", flush=True)

    try:
        # 终端和网页共用完整查询流程；本入口保留参数解析、输出和 trace 文件路径。
        execution = execute_query(
            question, live=args.live, database=args.database, sql=args.sql, table=table,
            max_model_turns=args.max_model_turns, schema_memory=args.schema_memory,
            schema_retrieval=args.schema_retrieval, schema_expand=args.schema_expand,
            conversation_id=args.conversation_id, history_turns=args.history_turns,
            experience_memory=args.experience_memory,
            on_prepared=None if args.json else print_query_header,
            max_tool_calls=args.max_tool_calls, max_sql_attempts=args.max_sql_attempts,
        )
    except ImportError:
        parser.exit(1, "Install project dependencies in .venv with: uv pip install --python .venv/Scripts/python.exe -e .\n")
    except (ValueError, RuntimeError, OSError, TypeError, KeyError) as error:
        parser.exit(1, f"Error: {error}\n")
    result = execution.result
    trace_payload = execution.payload
    saved_path = execution.conversation_saved_path
    if args.trace_file:
        trace_path = Path(args.trace_file)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        # 保存完整事件，而不是只保存最终答案，方便后续页面展示 SQL 和工具结果。
        trace_path.write_text(
            json.dumps(trace_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(trace_payload, ensure_ascii=False, indent=2))
    else:
        for event in result.events:
            # 终端显示事件摘要；包含表格的 metadata 完整保留在 trace JSON 中。
            summary = {key: value for key, value in event.details.items() if key != "metadata"}
            print(f"{event.model_turn:02d} {event.kind}: {json.dumps(summary)}")
        if result.stop_reason != "error":
            print(f"Answer: {result.final_answer}")
        print(f"Stop reason: {result.stop_reason}")
        if saved_path is not None:
            print(f"Conversation saved: {saved_path}")
    if result.stop_reason == "error":
        # 先按原参数保存 trace，再用原来的异常退出码报告；--json 的标准输出仍是完整 JSON。
        parser.exit(1, f"Error: {result.error}\n")
    return 0 if result.stop_reason == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

