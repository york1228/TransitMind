"""Fuse local vector candidates with one-hop schema graph candidates."""

import argparse
import json
from typing import Any

from .schema_graph import GraphSchemaRetriever
from .schema_store import JsonSchemaStore
from .schema_vectors import VectorSchemaRetriever


RRF_K = 60
CANDIDATE_LIMIT = 5
SEED_LIMIT = 2

# RRF 就是一个“排名融合算法”。
def reciprocal_rank_fusion(
    rankings: dict[str, list[dict[str, Any]]], k: int = RRF_K,
) -> list[dict[str, Any]]:
    fused = {}
    for source, matches in rankings.items():
        for rank, item in enumerate(matches, start=1):
            table = item["table"]
            candidate = fused.setdefault(table, {
                "table": table, "score": 0.0, "source_ranks": {}, "source_scores": {},
            })
            if source in candidate["source_ranks"]:
                continue
            # 只融合名次，不直接相加余弦相似度和图距离分数；名次从 1 开始。
            candidate["score"] += 1.0 / (k + rank)
            candidate["source_ranks"][source] = rank
            candidate["source_scores"][source] = item["score"]
    # 同分按表名稳定排序；RRF 分数不是概率，也不证明 SQL 正确。
    return sorted(fused.values(), key=lambda item: (-item["score"], item["table"]))


class HybridSchemaRetriever:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        # 两路绑定同一快照；复用现有索引、模型缓存和 Neo4j 配置，不新建存储。
        self.snapshot = snapshot
        self.vector = VectorSchemaRetriever(snapshot)
        self.graph = GraphSchemaRetriever(snapshot)
        self.model_name = self.vector.model_name

    def retrieve(self, query: str, limit: int = 3) -> dict[str, Any]:
        vector_matches = self.vector.search(query, limit=CANDIDATE_LIMIT)
        # 图入口来自向量前两名，不再要求问题命中人工关键词；入口仍可能选错。
        seeds = [item["table"] for item in vector_matches[:SEED_LIMIT]]
        graph_result = self.graph.retrieve_from_seeds(seeds, limit=CANDIDATE_LIMIT)
        graph_matches = graph_result["matches"]
        rankings = {"vector": vector_matches, "graph": graph_matches}
        fused = reciprocal_rank_fusion(rankings)[:limit]

        # 两路命中同一张表时只返回一份 Schema，优先保留从图取回的同版本元数据。
        tables = {item["table"]: item for item in vector_matches}
        graph_tables = {item["table"]: item for item in graph_matches}
        tables.update(graph_tables)
        matches = []
        for candidate in fused:
            name = candidate["table"]
            graph_table = graph_tables.get(name)
            matches.append({
                **tables[name], **candidate,
                "retrieval_sources": list(candidate["source_ranks"]),
                "hop_count": graph_table["hop_count"] if graph_table is not None else None,
                "is_seed": name in seeds,
            })
        selected = {item["table"] for item in matches}
        return {
            "schema_version": self.snapshot["version"], "embedding_model": self.model_name,
            "score_type": "rrf", "rrf_k": RRF_K, "matches": matches,
            "seed_tables": seeds, "graph_synced_at": graph_result["graph_synced_at"],
            "relationships": [
                relationship for relationship in graph_result["relationships"]
                if relationship["from_table"] in selected and relationship["to_table"] in selected
            ],
            # 完整候选名次留给 trace/本地 CLI，不再给模型重复发送两路完整 Schema。
            "rankings": {
                source: [
                    {"table": item["table"], "rank": rank, "score": item["score"]}
                    for rank, item in enumerate(items, start=1)
                ]
                for source, items in rankings.items()
            },
            "retrieval_config": {
                "candidate_limit": CANDIDATE_LIMIT, "seed_limit": SEED_LIMIT,
                "final_limit": limit,
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Search schema with vector, graph and RRF fusion.")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="Use local embedding and read Neo4j; no DeepSeek or PostgreSQL.")
    search.add_argument("--query", required=True)
    args = parser.parse_args()
    try:
        result = HybridSchemaRetriever(JsonSchemaStore().load()).retrieve(args.query)
        print("Mode: schema_hybrid_search (local embedding + one-hop Neo4j read + RRF; no DeepSeek or PostgreSQL)")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
