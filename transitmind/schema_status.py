"""Report local schema/index version evidence without contacting a database."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema_store import DEFAULT_FILE, JsonSchemaStore


GRAPH_SYNC_RECEIPT = DEFAULT_FILE.with_name("graph-sync.json")


def _read_index_status(
    path: Path, snapshot: dict[str, Any], *, missing_status: str,
) -> dict[str, Any]:
    status: dict[str, Any] = {"status": missing_status, "schema_version": None, "recorded_at": None}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return status
    except (OSError, ValueError):
        return {**status, "status": "unreadable"}
    scope = ("database_id", "schema", "schema_version")
    if not isinstance(record, dict) or not all(isinstance(record.get(key), str) for key in scope):
        return {**status, "status": "unreadable"}
    # 这里只比较记录中的范围与版本，不运行向量检索或验证远端图的可用性。
    matched = tuple(record[key] for key in scope) == (
        snapshot["database_id"], snapshot["schema"], snapshot["version"],
    )
    return {
        "status": "matched" if matched else "outdated",
        "schema_version": record["schema_version"],
        "recorded_at": record.get("built_at") or record.get("completed_at"),
    }


def get_schema_status(store: JsonSchemaStore | None = None) -> dict[str, Any]:
    from .schema_vectors import DEFAULT_INDEX

    snapshot = (store or JsonSchemaStore()).load()
    vector = _read_index_status(DEFAULT_INDEX, snapshot, missing_status="missing")
    graph = _read_index_status(GRAPH_SYNC_RECEIPT, snapshot, missing_status="unrecorded")
    if graph["status"] == "matched":
        # 同版本的同步凭据，不等于当前 Neo4j 在线或其中的数据仍存在。
        graph["status"] = "recorded"
    return {
        "database_id": snapshot["database_id"], "schema": snapshot["schema"],
        "snapshot_version": snapshot["version"],
        "indexes": {
            "vector": {**vector, "command": r".\.venv\Scripts\python.exe -m transitmind.schema_vectors build"},
            "graph": {**graph, "remote_checked": False, "command": r".\.venv\Scripts\python.exe -m transitmind.schema_graph sync"},
        },
    }


def save_graph_sync_receipt(snapshot: dict[str, Any], neo4j_database: str) -> None:
    # 只在 Neo4j 提交成功后调用，不把密码、连接字符串或业务记录写进凭据。
    receipt = {
        "database_id": snapshot["database_id"], "schema": snapshot["schema"],
        "schema_version": snapshot["version"], "neo4j_database": neo4j_database,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    GRAPH_SYNC_RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    temporary = GRAPH_SYNC_RECEIPT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(GRAPH_SYNC_RECEIPT)


def main() -> int:
    parser = argparse.ArgumentParser(description="Show local schema/index version records without model or database requests.")
    parser.parse_args()
    try:
        print(json.dumps(get_schema_status(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
