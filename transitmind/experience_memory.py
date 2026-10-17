"""Save manually approved query references and retrieve them by keywords."""

import argparse
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Message, QueryExperience
from .database_config import EXPECTED_DATABASE


DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "memory" / "experiences.json"


class JsonExperienceStore:
    def __init__(self, path: Path = DEFAULT_FILE) -> None:
        self.path = path

    def load(self) -> list[QueryExperience]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        # JSON 中的关键词是列表，恢复成数据模型里的元组。
        return [
            QueryExperience(**{**item, "keywords": tuple(item["keywords"])})
            for item in data
        ]

    def save(self, experience: QueryExperience) -> None:
        keywords = tuple(dict.fromkeys(word.strip() for word in experience.keywords if word.strip()))
        if not all((experience.id, experience.database_id, experience.question, experience.notes, keywords)):
            raise ValueError("Provide id, database_id, question, notes and at least one keyword.")
        # 只有人工执行保存命令才写入经验；正常 Agent 查询不会自动保存模型输出。
        experience = replace(
            experience, keywords=keywords,
            approved_at=datetime.now(timezone.utc).isoformat(),
        )
        # 同一业务库、同一 ID 表示更新这条经验，其他记录继续保留。
        records = [
            item for item in self.load()
            if (item.database_id, item.id) != (experience.database_id, experience.id)
        ]
        records.append(experience)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)

    def search(self, query: str, database_id: str, limit: int = 3) -> list[dict[str, Any]]:
        text = query.casefold()
        matches = []
        for experience in self.load():
            # 按业务库隔离，并只使用有人工认可时间的记录。
            if experience.database_id != database_id or not experience.approved_at:
                continue
            matched_keywords = [word for word in experience.keywords if word.casefold() in text]
            if matched_keywords:
                matches.append({
                    **asdict(experience),
                    "score": len(matched_keywords),
                    "matched_keywords": matched_keywords,
                })
        # 第一版按命中关键词数量排序；这不是向量相似度，后续再替换检索方法。
        matches.sort(key=lambda item: (-item["score"], item["id"]))
        return matches[:limit]


def build_experience_context(matches: list[dict[str, Any]]) -> tuple[Message, ...]:
    if not matches:
        return ()
    # 经验作为单独的参考消息传给模型，不伪装成数据库工具的真实查询结果。
    content = {
        "context_type": "approved_query_experiences",
        "reference_only": True,
        "matches": matches,
    }
    return (Message("user", json.dumps(content, ensure_ascii=False)),)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage local, manually approved query experiences.")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Save a reference you have reviewed; does not execute its SQL.")
    add.add_argument("--file", type=Path, required=True, help="JSON file containing one query experience.")
    search = commands.add_parser("search", help="Show keyword matches without calling a model or database.")
    search.add_argument("--query", required=True)
    search.add_argument("--database-id", default=EXPECTED_DATABASE)
    args = parser.parse_args()
    store = JsonExperienceStore()
    try:
        if args.command == "add":
            data = json.loads(args.file.read_text(encoding="utf-8"))
            experience = QueryExperience(**{**data, "keywords": tuple(data["keywords"])})
            store.save(experience)
            print("Mode: experience_save (local file write; no database or LLM)")
            print(f"Saved experience: {experience.id} | Database: {experience.database_id}")
            print(f"Store: {store.path}")
            print("Reference approved and stored. Its SQL was not executed or validated by this command.")
        else:
            matches = store.search(args.query, args.database_id)
            print("Mode: experience_search (local keywords; no database or LLM)")
            print(json.dumps(matches, ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
