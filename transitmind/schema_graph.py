"""Sync a versioned schema graph and retrieve its one-hop neighbors."""

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from .neo4j_store import Neo4jSchemaStore, SchemaGraphError, load_neo4j_settings
from .schema_memory import search_schema
from .schema_store import JsonSchemaStore


def graph_id(snapshot: dict[str, Any], *parts: str) -> str:
    # ID 包含业务库、命名空间和版本；不同版本不会互相覆盖，也不删除旧图。
    return json.dumps(
        ("TransitMind", snapshot["database_id"], snapshot["schema"], snapshot["version"], *parts),
        ensure_ascii=False, separators=(",", ":"),
    )


def build_graph_records(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot_id = graph_id(snapshot, "snapshot")
    scope = {
        "project": "TransitMind", "database_id": snapshot["database_id"],
        "schema_name": snapshot["schema"], "version": snapshot["version"],
        "snapshot_id": snapshot_id,
    }
    records: dict[str, Any] = {
        "snapshot": {
            **scope, "id": snapshot_id, "schema_synced_at": snapshot["synced_at"],
            "graph_synced_at": datetime.now(timezone.utc).isoformat(),
        },
        "tables": [], "columns": [], "table_links": [], "column_links": [],
        "skipped_foreign_keys": [],
    }
    columns_by_table = {}
    foreign_key_groups = {}
    for table in snapshot["tables"]:
        name = table["table"]
        table_id = graph_id(snapshot, "table", name)
        records["tables"].append({
            **scope, "id": table_id, "name": name,
            "description": table["description"], "keywords": table["keywords"],
            "primary_key": table["primary_key"],
            # 嵌套结构编码为 JSON；查询时从 Neo4j 取回，而不是把本地结果冒充成图检索。
            "metadata_json": json.dumps(table, ensure_ascii=False),
        })
        columns_by_table[name] = set(table["columns"])
        for column in table["column_details"]:
            records["columns"].append({
                **scope, "id": graph_id(snapshot, "column", name, column["name"]),
                "table_id": table_id, "table_name": name,
                "name": column["name"], "data_type": column["data_type"],
                "nullable": column["nullable"], "description": column["description"],
                "is_primary_key": column["name"] in table["primary_key"],
            })
        for fk in table["foreign_keys"]:
            key = (name, fk["constraint"], fk["referenced_schema"], fk["referenced_table"])
            foreign_key_groups.setdefault(key, []).append(fk)

    for (name, constraint, target_schema, target), keys in foreign_key_groups.items():
        # 复合外键按 position 保留两侧字段的对应顺序，不把各列当作独立连接条件。
        keys.sort(key=lambda fk: fk["position"])
        if (
            target_schema != snapshot["schema"] or target not in columns_by_table
            or any(
                fk["column"] not in columns_by_table[name]
                or fk["referenced_column"] not in columns_by_table[target]
                for fk in keys
            )
        ):
            records["skipped_foreign_keys"].append({
                "table": name, "constraint": constraint,
                "referenced_schema": target_schema, "referenced_table": target,
            })
            continue
        records["table_links"].append({
            "from_id": graph_id(snapshot, "table", name),
            "to_id": graph_id(snapshot, "table", target), "constraint": constraint,
            "columns": [fk["column"] for fk in keys],
            "referenced_columns": [fk["referenced_column"] for fk in keys],
        })
        for fk in keys:
            records["column_links"].append({
                "from_id": graph_id(snapshot, "column", name, fk["column"]),
                "to_id": graph_id(snapshot, "column", target, fk["referenced_column"]),
                "constraint": constraint, "position": fk["position"],
            })
    return records


class GraphSchemaRetriever:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        self.store = Neo4jSchemaStore(load_neo4j_settings())

    def retrieve(self, query: str, limit: int = 3) -> dict[str, Any]:
        # graph 模式使用关键词入口；混合模式会另行提供向量召回的入口表。
        seeds = [item["table"] for item in search_schema(self.snapshot, query, limit)]
        # 用问题中的关键词，在已有 Schema 快照里找到入口表。
        return self.retrieve_from_seeds(seeds, limit)

    def retrieve_from_seeds(self, seeds: list[str], limit: int = 3) -> dict[str, Any]:
        # 复用同一图查询；入口来自关键词、向量召回，或 schema_expand 接收的已知表名。
        seeds = list(dict.fromkeys(seeds))
        rows = self.store.neighborhood(graph_id(self.snapshot, "snapshot"), seeds)
        candidates = {}
        relationships = {}
        for row in rows:
            if row["seed_table"] is not None:
                candidates[row["seed_table"]] = {
                    **json.loads(row["seed_metadata"]), "hop_count": 0, "is_seed": True,
                }
            if row["neighbor_table"] is not None:
                candidates.setdefault(row["neighbor_table"], {
                    **json.loads(row["neighbor_metadata"]), "hop_count": 1, "is_seed": False,
                })
            if row["constraint"] is not None:
                key = (row["from_table"], row["constraint"], row["to_table"])
                relationships[key] = {
                    "from_table": row["from_table"], "to_table": row["to_table"],
                    "constraint": row["constraint"], "columns": row["columns"],
                    "referenced_columns": row["referenced_columns"],
                }
        seed_order = {name: index for index, name in enumerate(seeds)}
        matches = sorted(
            candidates.values(),
            key=lambda item: (
                item["hop_count"], seed_order.get(item["table"], len(seeds)), item["table"],
            ),
        )[:limit]
        for table in matches:
            # 仅表示距离：入口表为 1，一层邻居为 0.5，不是概率或 SQL 正确率。
            table["score"] = 1.0 / (1 + table["hop_count"])
        selected = {table["table"] for table in matches}
        return {
            "schema_version": self.snapshot["version"],
            "graph_synced_at": rows[0]["graph_synced_at"],
            "seed_tables": seeds, "score_type": "graph_proximity", "matches": matches,
            "relationships": [
                relationship for relationship in relationships.values()
                if relationship["from_table"] in selected and relationship["to_table"] in selected
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync or search the local Neo4j schema graph.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync", help="Write the saved schema snapshot to Neo4j; no business rows.")
    search = commands.add_parser("search", help="Read one-hop schema neighbors from Neo4j.")
    search.add_argument("--query", required=True)
    args = parser.parse_args()
    try:
        snapshot = JsonSchemaStore().load()
        if args.command == "sync":
            settings = load_neo4j_settings()
            records = build_graph_records(snapshot)
            Neo4jSchemaStore(settings).sync(records)
            # 图已提交后再记录本地凭据；页面只读这份记录，不自动探测 Neo4j。
            from .schema_status import save_graph_sync_receipt

            try:
                save_graph_sync_receipt(snapshot, settings.database)
            except OSError:
                print("Warning: Neo4j sync committed, but its local status receipt could not be saved.")
            print("Mode: schema_graph_sync (Neo4j metadata write; no PostgreSQL or LLM request)")
            print(f"Neo4j database: {settings.database} | Business database: {snapshot['database_id']}")
            print(f"Schema version: {snapshot['version']}")
            print(f"Committed table records: {len(records['tables'])} | Column records: {len(records['columns'])}")
            print(f"Table FK records: {len(records['table_links'])} | Column FK records: {len(records['column_links'])}")
            print(f"Skipped FK targets: {json.dumps(records['skipped_foreign_keys'], ensure_ascii=False)}")
            print("No business rows were copied. Existing graph data was not deleted.")
        else:
            result = GraphSchemaRetriever(snapshot).retrieve(args.query)
            print("Mode: schema_graph_search (keyword seeds + one-hop Neo4j read; no PostgreSQL or LLM request)")
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
