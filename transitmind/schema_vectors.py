"""Build local schema embeddings and rank tables by cosine similarity."""

import argparse
import json
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from typing import Any

from .schema_store import PROJECT_ROOT, JsonSchemaStore


DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_INDEX = PROJECT_ROOT / "data" / "memory" / "schema" / "vectors.json"
MODEL_CACHE = PROJECT_ROOT / "data" / "models" / "fastembed"
INSTALL_HINT = 'Install vector dependencies: uv pip install --python .venv/Scripts/python.exe -e ".[vector]"'


def table_document(table: dict[str, Any], schema: str) -> str:
    # 它把表名、业务说明、字段、主外键拼成一段文本，作为 Embedding 的输入。

    # 向量模型读取表的业务含义和结构说明，不读取数据库中的业务记录。
    lines = [
        f"表：{schema}.{table['table']}",
        f"业务含义：{table['description']}",
        f"相关词：{'、'.join(table['keywords'])}",
        f"字段：{', '.join(table['columns'])}",
    ]
    lines.extend(
        f"{column['name']}：{column['description']}"
        for column in table["column_details"] if column.get("description")
    )
    lines.append(f"主键：{', '.join(table['primary_key'])}")
    lines.extend(
        f"关联：{key['column']} -> {key['referenced_schema']}.{key['referenced_table']}.{key['referenced_column']}"
        for key in table["foreign_keys"]
    )
    return "\n".join(lines)


def load_embedding_model(model_name: str, *, local_files_only: bool = False) -> Any:
    # 只在向量功能中导入可选依赖，原关键词模式不用安装或加载 Embedding 模型。
    try:
        from fastembed import TextEmbedding
    except ImportError:
        raise ValueError(INSTALL_HINT) from None
    return TextEmbedding(
        model_name=model_name, cache_dir=str(MODEL_CACHE),
        providers=["CPUExecutionProvider"], local_files_only=local_files_only,
    )


def build_vector_index(snapshot: dict[str, Any], model_name: str = DEFAULT_MODEL) -> dict[str, Any]:
    # 将这些描述交给 FastEmbed，每张表生成一个向量并保存。这里的神经网络推理由现成库完成，我们负责输入组装和索引保存
    documents = [table_document(table, snapshot["schema"]) for table in snapshot["tables"]]
    print(f"Loading local embedding model: {model_name} (first use downloads model files)...", flush=True)
    model = load_embedding_model(model_name)
    # 神经网络编码由 FastEmbed 完成；每张表生成一个向量，转成列表后才能写入 JSON。
    vectors = [vector.tolist() for vector in model.passage_embed(documents)]
    return {
        "database_id": snapshot["database_id"],
        "schema": snapshot["schema"],
        "schema_version": snapshot["version"],
        "model_name": model_name,
        "dimensions": len(vectors[0]),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "entries": [
            {"table": table["table"], "document": document, "vector": vector}
            for table, document, vector in zip(snapshot["tables"], documents, vectors, strict=True)
        ],
    }


def save_vector_index(index: dict[str, Any], path: Path = DEFAULT_INDEX) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    # 比较两个向量的方向；分数是相似度，不是 SQL 正确率或概率。
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    length = sqrt(sum(value * value for value in left) * sum(value * value for value in right))
    return dot / length if length else 0.0


class VectorSchemaRetriever:
    def __init__(self, snapshot: dict[str, Any], path: Path = DEFAULT_INDEX) -> None:
        if not path.exists():
            raise ValueError("No schema vector index. Run python -m transitmind.schema_vectors build first.")
        self.index = json.loads(path.read_text(encoding="utf-8"))
        if (self.index["database_id"], self.index["schema"], self.index["schema_version"]) != (
            snapshot["database_id"], snapshot["schema"], snapshot["version"],
        ):
            raise ValueError("Schema snapshot changed. Rebuild its index: python -m transitmind.schema_vectors build")
        self.tables = {table["table"]: table for table in snapshot["tables"]}
        self.model_name = self.index["model_name"]
        self._model = None

    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        if self._model is None:
            # 查询时使用已经下载的模型；同一提问中的多次工具调用复用这个实例。
            self._model = load_embedding_model(self.model_name, local_files_only=True)
        query_vector = next(iter(self._model.query_embed(query))).tolist()
        matches = [
            {**self.tables[entry["table"]], "score": cosine_similarity(query_vector, entry["vector"])}
            for entry in self.index["entries"]
            # 加载本地模型并编码当前问题。 同一次提问的多次工具调用会复用模型实例。
            # 逐张表比较向量。 entry["vector"] 是构建索引时保存的表向量；query_vector 是现在生成的问题向量。
            # 按相似度排序。 最多返回三张候选表，并带上它们的完整结构。
        ]
        # 当前只有七张表，直接逐个比较。以后换向量数据库时仍可保留 search 接口。
        matches.sort(key=lambda item: (-item["score"], item["table"]))
        return matches[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or search a local schema vector index.")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Embed saved schema metadata; the first run downloads a local model.")
    build.add_argument("--model", default=DEFAULT_MODEL, help="A FastEmbed-supported text embedding model.")
    search = commands.add_parser("search", help="Run vector retrieval without DeepSeek or a database request.")
    search.add_argument("--query", required=True)
    args = parser.parse_args()
    try:
        snapshot = JsonSchemaStore().load()
        if args.command == "build":
            index = build_vector_index(snapshot, args.model)
            save_vector_index(index)
            print("Mode: schema_vector_build (local embedding; no database or DeepSeek request)")
            print(f"Schema version: {index['schema_version']}")
            print(f"Model: {index['model_name']} | Dimensions: {index['dimensions']} | Tables: {len(index['entries'])}")
            print(f"Index: {DEFAULT_INDEX}")
            print(f"Model cache: {MODEL_CACHE}")
        else:
            retriever = VectorSchemaRetriever(snapshot)
            matches = retriever.search(args.query)
            print("Mode: schema_vector_search (local embedding; no database or DeepSeek request)")
            print(json.dumps({
                "schema_version": snapshot["version"], "model_name": retriever.model_name,
                "score_type": "cosine_similarity", "matches": matches,
            }, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
