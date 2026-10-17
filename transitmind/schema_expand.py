"""Expand known table names through the saved Neo4j schema graph."""

import argparse
import json
from functools import partial
from typing import Any

from .models import ToolDefinition, ToolResult
from .neo4j_store import SchemaGraphError
from .schema_graph import GraphSchemaRetriever
from .schema_store import JsonSchemaStore
from .tools import Tool


SEED_LIMIT = 2
TABLE_LIMIT = 3


def schema_expand(
    snapshot: dict[str, Any], arguments: dict[str, Any], *, retriever: GraphSchemaRetriever,
) -> ToolResult:
    # 模型提供已知表名；这里把一次扩展限制为一到两个入口、一层外键关系。
    seeds = arguments.get("seed_tables")
    if (
        set(arguments) != {"seed_tables"} or not isinstance(seeds, list)
        or not 1 <= len(seeds) <= SEED_LIMIT
        or any(not isinstance(name, str) or not name.strip() for name in seeds)
    ):
        return ToolResult(False, "Provide seed_tables as an array of one or two nonempty table names.", "invalid_arguments")
    seeds = list(dict.fromkeys(name.strip() for name in seeds))
    known_tables = {table["table"] for table in snapshot["tables"]}
    unknown = [name for name in seeds if name not in known_tables]
    if unknown:
        return ToolResult(
            False, f"Tables not in this schema snapshot: {', '.join(unknown)}. Use schema_retrieve or describe_table.",
            "unknown_table",
        )

    try:
        # 直接使用表名读取图，不再编码问题、检索向量或计算 RRF。
        result = retriever.retrieve_from_seeds(seeds, limit=TABLE_LIMIT)
    except SchemaGraphError as error:
        return ToolResult(False, str(error), "schema_graph_error")

    config = {"seed_limit": SEED_LIMIT, "max_hops": 1, "table_limit": TABLE_LIMIT}
    content = {
        "source": "neo4j_schema_graph", "database_id": snapshot["database_id"],
        "schema": snapshot["schema"], "synced_at": snapshot["synced_at"],
        "retrieval_mode": "expand", "retrieval_config": config, **result,
    }
    # content 让模型判断关联；metadata 让终端 trace 和后续页面保留扩展依据。
    return ToolResult(
        True, json.dumps(content, ensure_ascii=False),
        metadata={
            "schema_version": result["schema_version"], "retrieval_mode": "expand",
            "score_type": result["score_type"], "seed_tables": result["seed_tables"],
            "graph_synced_at": result["graph_synced_at"], "relationships": result["relationships"],
            "matched_tables": [item["table"] for item in result["matches"]],
            "scores": [{"table": item["table"], "score": item["score"]} for item in result["matches"]],
            "retrieval_config": config,
        },
    )


def build_schema_expand_tool(snapshot: dict[str, Any]) -> Tool:
    # 注册时绑定同一份快照；模型调用时只补充 seed_tables 参数。
    retriever = GraphSchemaRetriever(snapshot)
    return Tool(
        definition=ToolDefinition(
            name="schema_expand",
            description=(
                "Expand one or two already-known, unqualified table names through one-hop foreign-key "
                "relationships in the saved Neo4j schema graph. Use when related tables or connection columns "
                "are needed; skip when existing schema evidence is sufficient. Returns up to three tables "
                "including seeds, column metadata, connection columns and a snapshot version. "
                "Graph distance is not semantic relevance. Does not read business rows or execute SQL, "
                "and relationships do not authorize joins outside run_sql's allowed scope."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "seed_tables": {
                        "type": "array", "items": {"type": "string", "minLength": 1},
                        "minItems": 1, "maxItems": SEED_LIMIT, "uniqueItems": True,
                    },
                },
                "required": ["seed_tables"], "additionalProperties": False,
            },
        ),
        handler=partial(schema_expand, snapshot, retriever=retriever),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read schema neighbors for known table names.")
    parser.add_argument("--tables", nargs="+", required=True, help="One or two known unqualified table names.")
    args = parser.parse_args()
    try:
        snapshot = JsonSchemaStore().load()
        tool = build_schema_expand_tool(snapshot)
        result = tool.handler({"seed_tables": args.tables})
        if not result.success:
            parser.exit(2, f"Error: {result.content}\n")
        print("Mode: schema_expand (known table seeds + one-hop Neo4j read; no embedding, PostgreSQL or LLM request)")
        print(json.dumps(json.loads(result.content), ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
