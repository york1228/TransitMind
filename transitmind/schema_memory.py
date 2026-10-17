"""Collect PostgreSQL schema snapshots and retrieve candidate tables."""

import argparse
import json
from datetime import datetime, timezone
from functools import partial
from importlib.resources import files
from pathlib import Path
from typing import Any

from .database_config import DatabaseSettings, load_database_settings
from .models import ToolDefinition, ToolResult
from .postgres_schema import describe_table
# 兼容旧导入路径；存储实现和原路径常量统一由 schema_store 提供。
from .schema_store import DEFAULT_FILE, PROJECT_ROOT, JsonSchemaStore
from .sql_policy import ALLOWED_TABLES
from .tools import Tool


# 从正式包内资源读取默认说明，安装后运行也不需要 docs 目录。
DEFAULT_NOTES = files("transitmind").joinpath("resources", "gtfs-schema-notes.json")


def collect_schema(
    settings: DatabaseSettings, notes: dict[str, Any], *, manual_annotations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tables = []
    missing_tables = []
    for table in sorted(ALLOWED_TABLES):
        # 复用真实结构工具；不读取原始 GTFS 文件，也不扫描 stop_times 的业务记录。
        result = describe_table(settings, {"table": table})
        if not result.success:
            if result.error_code == "unknown_table":
                missing_tables.append(table)
                continue
            raise ValueError(f"Cannot sync {table}: {result.content}")
        metadata = json.loads(result.content)
        defaults = notes.get(table, {})
        manual = (manual_annotations or {}).get(table, {})
        # 人工保存的说明优先于示例配置；真实列名、类型和键仍然来自数据库。
        annotation = {**defaults, **manual}
        column_notes = {**defaults.get("columns", {}), **manual.get("columns", {})}
        # metadata 是数据库给的结构，annotation 是我们补充的说明。 下一段 tables.append(...)，就是把这两份内容合成一条表结构记录。
        tables.append({
            "table": metadata["table"],
            # 中文说明来自人工配置；列名、类型、主外键仍以数据库读取结果为准。
            "description": annotation.get("description", ""),
            "keywords": annotation.get("keywords", []),
            "columns": metadata["columns"],
            "column_details": [
                {**column, "description": column_notes.get(column["name"], "")}
                for column in metadata["column_details"]
            ],
            "primary_key": metadata["primary_key"],
            "foreign_keys": metadata["foreign_keys"],
        })
    if not tables:
        raise ValueError("No visible allowed tables were found. Complete the database import and reader grants.")
    now = datetime.now(timezone.utc)
    return {
        "source": "postgresql_metadata",
        "database_id": settings.database,
        "schema": "public",
        "version": now.strftime("%Y%m%dT%H%M%S%fZ"),
        "synced_at": now.isoformat(),
        "tables": tables,
        "missing_tables": missing_tables,
        "manual_annotations": manual_annotations or {},
    }


def search_schema(snapshot: dict[str, Any], query: str, limit: int = 3) -> list[dict[str, Any]]:
    text = query.casefold()
    matches = []
    for table in snapshot["tables"]:
        # 中文关键词、真实表名和字段名都可以命中；这一步尚不是语义或向量检索。
        terms = dict.fromkeys([table["table"], *table["keywords"], *table["columns"]])
        matched_keywords = [word for word in terms if word and word.casefold() in text]
        if matched_keywords:
            matches.append({
                **table,
                "score": len(matched_keywords),
                "matched_keywords": matched_keywords,
            })
    matches.sort(key=lambda item: (-item["score"], item["table"]))
    return matches[:limit]


def schema_retrieve(
    snapshot: dict[str, Any], arguments: dict[str, Any], *,
    retrieval_mode: str = "keyword", vector_retriever: Any = None, graph_retriever: Any = None,
    hybrid_retriever: Any = None,
) -> ToolResult:
    if set(arguments) != {"query"} or not isinstance(arguments.get("query"), str) or not arguments["query"].strip():
        return ToolResult(False, "Provide exactly one nonempty string argument named query.", "invalid_arguments")
    retrieval_result = None
    if retrieval_mode in {"graph", "hybrid"}:
        from .neo4j_store import SchemaGraphError

        retriever = graph_retriever if retrieval_mode == "graph" else hybrid_retriever
        try:
            retrieval_result = retriever.retrieve(arguments["query"])
        except SchemaGraphError as error:
            return ToolResult(False, str(error), "schema_graph_error")
        matches = retrieval_result["matches"]
    elif retrieval_mode == "vector":
        matches = vector_retriever.search(arguments["query"])
    else:
        matches = search_schema(snapshot, arguments["query"])
    score_type = {
        "keyword": "keyword_hits", "vector": "cosine_similarity", "graph": "graph_proximity",
        "hybrid": "rrf",
    }[retrieval_mode]
    # 包成工具结果回到原 Agent loop；版本号说明这不是实时查询到的业务记录。
    content = {
        "source": {
            "graph": "neo4j_schema_graph", "hybrid": "vector_and_neo4j_schema",
        }.get(retrieval_mode, "schema_memory"),
        "database_id": snapshot["database_id"],
        "schema": snapshot["schema"],
        "schema_version": snapshot["version"],
        "synced_at": snapshot["synced_at"],
        "retrieval_mode": retrieval_mode,
        "score_type": score_type,
        "matches": matches,
    }
    if vector_retriever is not None:
        content["embedding_model"] = vector_retriever.model_name
    graph_metadata = {}
    if retrieval_result is not None:
        # 这些连接依据同时给模型和 trace；不改变 run_sql 的关联白名单。
        graph_metadata = {
            key: retrieval_result[key] for key in ("seed_tables", "relationships", "graph_synced_at")
        }
        content.update(graph_metadata)
    hybrid_metadata = {}
    if retrieval_mode == "hybrid":
        content.update({
            "embedding_model": hybrid_retriever.model_name, "rrf_k": retrieval_result["rrf_k"],
        })
        # 原始排名与最终融合依据用于调试；模型只读取最多三张候选的 Schema。
        hybrid_metadata = {
            "embedding_model": hybrid_retriever.model_name, "rrf_k": retrieval_result["rrf_k"],
            "rankings": retrieval_result["rankings"],
            "retrieval_config": retrieval_result["retrieval_config"],
            "fusion_details": [
                {key: item[key] for key in ("table", "score", "source_ranks", "source_scores", "retrieval_sources")}
                for item in matches
            ],
        }
    return ToolResult(
        True, json.dumps(content, ensure_ascii=False),
        metadata={
            "schema_version": snapshot["version"], "retrieval_mode": retrieval_mode,
            "score_type": score_type, "matched_tables": [item["table"] for item in matches],
            "scores": [{"table": item["table"], "score": item["score"]} for item in matches],
            **graph_metadata,
            **hybrid_metadata,
        },
    )


def build_schema_tool(
    database_id: str, retrieval_mode: str = "keyword", *, snapshot: dict[str, Any] | None = None,
) -> Tool:
    # 一次提问固定使用启动时加载的版本，中途同步不会改变这一轮的 Schema 参考。
    if snapshot is None:
        snapshot = JsonSchemaStore().load()
    if snapshot["database_id"] != database_id or snapshot["schema"] != "public":
        raise ValueError("The saved schema snapshot does not match the configured business database and schema.")
    vector_retriever = None
    graph_retriever = None
    hybrid_retriever = None
    if retrieval_mode == "vector":
        from .schema_vectors import VectorSchemaRetriever

        vector_retriever = VectorSchemaRetriever(snapshot)
        search_description = (
            "Semantically search saved PostgreSQL schema metadata with a local embedding model. "
            "Supply a natural-language description of the current data question, not just guessed table names. "
            "Scores are cosine similarities, not confidence or SQL accuracy. "
        )
    elif retrieval_mode == "graph":
        from .schema_graph import GraphSchemaRetriever

        graph_retriever = GraphSchemaRetriever(snapshot)
        search_description = (
            "Find seed tables using Chinese keywords, table names or column names, then retrieve "
            "one-hop foreign-key neighbors from the saved Neo4j schema graph. "
            "Returns connection columns and graph distance, not semantic confidence. "
            "A graph relationship does not authorize SQL outside run_sql's allowed scope. "
        )
    elif retrieval_mode == "hybrid":
        from .schema_hybrid import HybridSchemaRetriever

        hybrid_retriever = HybridSchemaRetriever(snapshot)
        search_description = (
            "Search saved schema metadata using local vector retrieval, take the top two vector tables "
            "as seeds for one-hop Neo4j traversal, then fuse both candidate rankings with RRF. "
            "Supply the natural-language data question in the user's language; known identifiers may supplement it. "
            "Scores are rank-fusion values, not probabilities or SQL accuracy. "
            "Graph relationships do not authorize SQL outside run_sql's allowed scope. "
        )
    elif retrieval_mode == "keyword":
        search_description = "Search saved PostgreSQL schema metadata using Chinese keywords, table names or column names. "
    else:
        raise ValueError("Choose keyword, vector, graph or hybrid schema retrieval.")
    return Tool(
        definition=ToolDefinition(
            name="schema_retrieve",
            description=(
                search_description +
                "Returns up to three candidate tables with columns, types, keys, manual descriptions and a "
                "snapshot version. Does not read business rows or execute SQL. No match does not prove that "
                "a table is absent; use describe_table when live metadata is needed."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        handler=partial(
            schema_retrieve, snapshot, retrieval_mode=retrieval_mode,
            vector_retriever=vector_retriever, graph_retriever=graph_retriever,
            hybrid_retriever=hybrid_retriever,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync or search the local schema memory.")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="Read database metadata and save a versioned local snapshot.")
    sync.add_argument("--notes", type=Path, help="External table descriptions and keywords; defaults to packaged MBTA notes.")
    search = commands.add_parser("search", help="Search the saved snapshot without a database or LLM request.")
    search.add_argument("--query", required=True)
    args = parser.parse_args()
    store = JsonSchemaStore()
    try:
        if args.command == "sync":
            # 显式指定 --notes 时用外部文件，否则读取包内默认值；人工覆盖仍在下方合并。
            notes_file = args.notes if args.notes is not None else DEFAULT_NOTES
            notes = json.loads(notes_file.read_text(encoding="utf-8"))
            previous = store.load() if store.path.exists() else {}
            snapshot = collect_schema(
                load_database_settings(for_query=True), notes,
                manual_annotations=previous.get("manual_annotations", {}),
            )
            if previous:
                snapshot["parent_version"] = previous["version"]
                snapshot["change"] = {"source": "database_sync"}
                if previous.get("annotations_updated_at"):
                    snapshot["annotations_updated_at"] = previous["annotations_updated_at"]
            store.save(snapshot)
            print("Mode: schema_sync (read-only PostgreSQL metadata; local file write; no LLM)")
            print(f"Database: {snapshot['database_id']} | Schema: {snapshot['schema']}")
            print(f"Schema version: {snapshot['version']} | Tables: {len(snapshot['tables'])}")
            print(f"Missing or invisible tables: {', '.join(snapshot['missing_tables']) or 'none'}")
            print(f"Store: {store.path}")
            print("No business rows were copied and no database tables were changed.")
        else:
            snapshot = store.load()
            matches = search_schema(snapshot, args.query)
            print("Mode: schema_search (saved metadata; no database or LLM request)")
            print(json.dumps({"schema_version": snapshot["version"], "matches": matches}, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
