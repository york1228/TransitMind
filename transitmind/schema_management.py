"""Edit schema annotations without changing database structure or business rows."""

from datetime import datetime, timezone
from threading import Lock
from typing import Any

from .schema_store import JsonSchemaStore


_EDIT_LOCK = Lock()


class SchemaVersionConflict(ValueError):
    """The editor was opened against an older schema snapshot."""


def update_table_annotations(
    store: JsonSchemaStore, table_name: str, *, expected_version: str,
    description: str, keywords: list[str], column_descriptions: dict[str, str],
) -> dict[str, Any]:
    # 同一工作台进程串行保存；版本检查防止旧页面覆盖刚保存的新说明。
    with _EDIT_LOCK:
        snapshot = store.load()
        if snapshot["version"] != expected_version:
            raise SchemaVersionConflict("Schema 版本已变化，请取消编辑并刷新后重试。")
        table = next((item for item in snapshot["tables"] if item["table"] == table_name), None)
        if table is None:
            raise KeyError(table_name)
        unknown = set(column_descriptions) - set(table["columns"])
        if unknown:
            raise ValueError(f"快照中没有这些字段：{', '.join(sorted(unknown))}")

        description = description.strip()
        keywords = list(dict.fromkeys(word.strip() for word in keywords if word.strip()))
        changes = []
        annotation_updates: dict[str, Any] = {}
        for name, value in (("description", description), ("keywords", keywords)):
            if table[name] != value:
                changes.append({"field": name, "before": table[name], "after": value})
                annotation_updates[name] = value
                table[name] = value
        column_updates = {}
        for column in table["column_details"]:
            name = column["name"]
            if name not in column_descriptions:
                continue
            value = column_descriptions[name].strip()
            if column.get("description", "") != value:
                changes.append({"field": f"columns.{name}", "before": column.get("description", ""), "after": value})
                column_updates[name] = value
                column["description"] = value
        if not changes:
            return {"changed": False, "snapshot": snapshot}

        # 人工覆盖与采集结果放在同一份快照中，避免多文件保存到一半的状态。
        # 重新采集真实字段时继续应用这些覆盖，不用改包内的默认业务说明。
        annotations = snapshot.setdefault("manual_annotations", {}).setdefault(table_name, {})
        annotations.update(annotation_updates)
        if column_updates:
            annotations.setdefault("columns", {}).update(column_updates)
        now = datetime.now(timezone.utc)
        snapshot["parent_version"] = snapshot["version"]
        snapshot["version"] = now.strftime("%Y%m%dT%H%M%S%fZ")
        snapshot["annotations_updated_at"] = now.isoformat()
        snapshot["change"] = {"source": "local_manual_edit", "table": table_name, "changes": changes}
        # synced_at 仍表示上次读取数据库结构的时间；这里只变更说明和版本。
        store.save(snapshot)
        return {"changed": True, "snapshot": snapshot}
