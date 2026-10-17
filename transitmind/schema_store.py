"""Read and save versioned local Schema snapshots."""

import json
from pathlib import Path
from typing import Any


# 模块仍位于 transitmind 包的同一层，项目根目录和原快照路径保持一致。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = PROJECT_ROOT / "data" / "memory" / "schema" / "current.json"


class JsonSchemaStore:
    def __init__(self, path: Path = DEFAULT_FILE) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise ValueError("No schema snapshot. Run python -m transitmind.schema_memory sync first.")
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, snapshot: dict[str, Any]) -> None:
        # 每次同步保留一个版本；current.json 只指向这次保存的最新内容。
        versions = self.path.parent / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
        (versions / f"{snapshot['version']}.json").write_text(payload, encoding="utf-8")
        # 先写临时文件再替换，和会话、经验记忆的保存方式一致。
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)
