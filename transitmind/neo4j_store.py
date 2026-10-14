"""Store schema metadata in the local TransitMind Neo4j instance."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv


class SchemaGraphError(RuntimeError):
    """A graph error that can be shown without exposing connection secrets."""


@dataclass(frozen=True)
class Neo4jSettings:
    uri: str
    user: str
    database: str
    password: str = field(repr=False)


def load_neo4j_settings() -> Neo4jSettings:
    # 只加载本项目的配置；图数据库密码与 PostgreSQL、DeepSeek 的凭据独立。
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    uri = os.getenv("TRANSITMIND_NEO4J_URI", "neo4j://127.0.0.1:7687").strip()
    try:
        address = urlsplit(uri)
        local = (
            address.scheme in {"neo4j", "bolt"}
            and address.hostname in {"localhost", "127.0.0.1", "::1"}
            and address.username is None and address.password is None
        )
    except ValueError:
        local = False
    if not local:
        raise SchemaGraphError("Use a local neo4j:// or bolt:// URI without embedded credentials.")
    user = os.getenv("TRANSITMIND_NEO4J_USER", "neo4j").strip()
    database = os.getenv("TRANSITMIND_NEO4J_DATABASE", "neo4j").strip()
    password = os.getenv("TRANSITMIND_NEO4J_PASSWORD", "")
    if not user or not database or not password:
        raise SchemaGraphError("Set the Neo4j username, database and password in TransitMind's local .env.")
    return Neo4jSettings(uri=uri, user=user, database=database, password=password)


CONSTRAINTS = (
    "CREATE CONSTRAINT transitmind_snapshot_id IF NOT EXISTS "
    "FOR (n:TransitMindSnapshot) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT transitmind_table_id IF NOT EXISTS "
    "FOR (n:TransitMindTable) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT transitmind_column_id IF NOT EXISTS "
    "FOR (n:TransitMindColumn) REQUIRE n.id IS UNIQUE",
)

WRITE_QUERIES = (
    ("tables", """
        UNWIND $rows AS row
        MATCH (snapshot:TransitMindSnapshot {id: row.snapshot_id})
        MERGE (table:TransitMindTable {id: row.id})
        SET table += row
        MERGE (snapshot)-[:HAS_TABLE]->(table)
    """),
    ("columns", """
        UNWIND $rows AS row
        MATCH (table:TransitMindTable {id: row.table_id})
        MERGE (column:TransitMindColumn {id: row.id})
        SET column += row
        MERGE (table)-[:HAS_COLUMN]->(column)
    """),
    ("table_links", """
        UNWIND $rows AS row
        MATCH (source:TransitMindTable {id: row.from_id})
        MATCH (target:TransitMindTable {id: row.to_id})
        MERGE (source)-[fk:REFERENCES_TABLE {constraint: row.constraint}]->(target)
        SET fk.columns = row.columns, fk.referenced_columns = row.referenced_columns
    """),
    ("column_links", """
        UNWIND $rows AS row
        MATCH (source:TransitMindColumn {id: row.from_id})
        MATCH (target:TransitMindColumn {id: row.to_id})
        MERGE (source)-[fk:REFERENCES {constraint: row.constraint, position: row.position}]->(target)
    """),
)

NEIGHBORHOOD_QUERY = """
    MATCH (snapshot:TransitMindSnapshot {id: $snapshot_id})
    OPTIONAL MATCH (snapshot)-[:HAS_TABLE]->(seed:TransitMindTable)
    WHERE seed.name IN $seeds
    OPTIONAL MATCH (seed)-[fk:REFERENCES_TABLE]-(neighbor:TransitMindTable)
    WHERE neighbor.snapshot_id = $snapshot_id
    RETURN snapshot.version AS schema_version,
           snapshot.graph_synced_at AS graph_synced_at,
           seed.name AS seed_table, seed.metadata_json AS seed_metadata,
           neighbor.name AS neighbor_table, neighbor.metadata_json AS neighbor_metadata,
           fk.constraint AS constraint, fk.columns AS columns,
           fk.referenced_columns AS referenced_columns,
           startNode(fk).name AS from_table, endNode(fk).name AS to_table
    ORDER BY seed_table, neighbor_table, constraint
"""
# OPTIONAL MATCH (seed)-[fk:REFERENCES_TABLE]-(neighbor:TransitMindTable)
# 找到入口表，再沿着一条外键关系，找相邻的表。 中间没有方向箭头，因此外键的两侧都可以作为查找入口；返回结果仍保留原来的外键方向和对应字段。

class Neo4jSchemaStore:
    def __init__(self, settings: Neo4jSettings) -> None:
        # 可选依赖延迟导入；keyword/vector 模式不需要安装 Neo4j 驱动。
        try:
            import neo4j
            from neo4j.exceptions import DriverError, Neo4jError
        except ImportError:
            raise SchemaGraphError(
                'Install graph dependencies: uv pip install --python .venv/Scripts/python.exe -e ".[graph]"'
            ) from None
        self.settings = settings
        self.neo4j = neo4j
        self.error_types = (DriverError, Neo4jError, ValueError)

    def _driver(self) -> Any:
        return self.neo4j.GraphDatabase.driver(
            self.settings.uri, auth=(self.settings.user, self.settings.password),
            connection_timeout=5, connection_acquisition_timeout=5,
            max_transaction_retry_time=0,
        )

    # sync() 把 schema_graph 的 build_graph_records() 准备节点和关系真正写入 Neo4j
    def sync(self, records: dict[str, Any]) -> None:
        try:
            with self._driver() as driver:
                with driver.session(database=self.settings.database) as session:
                    # 唯一约束单独提交；随后所有节点和关系在同一个数据事务中写入。
                    for statement in CONSTRAINTS:
                        session.run(self.neo4j.Query(statement, timeout=30)).consume()
                    with session.begin_transaction(timeout=30) as transaction:
                        transaction.run(
                            "MERGE (snapshot:TransitMindSnapshot {id: $properties.id}) "
                            "SET snapshot += $properties",
                            {"properties": records["snapshot"]},
                        ).consume()
                        for key, statement in WRITE_QUERIES:
                            transaction.run(statement, {"rows": records[key]}).consume()
                        transaction.commit()
                        # 快照、节点和关系在同一个数据事务中提交；
        except self.error_types as error:
            # 不把驱动的原始异常写进终端或 trace，避免泄露连接细节。
            raise SchemaGraphError(
                f"Neo4j sync failed ({type(error).__name__}). Check the running instance, "
                "database, credentials and permission to create constraints."
            ) from None

    def neighborhood(self, snapshot_id: str, seeds: list[str]) -> list[dict[str, Any]]:
        try:
            with self._driver() as driver:
                with driver.session(
                    database=self.settings.database, default_access_mode=self.neo4j.READ_ACCESS,
                ) as session:
                    # READ_ACCESS 是读取路由意图，不代替数据库账号权限；这里只执行固定查询。
                    with session.begin_transaction(timeout=5) as transaction:
                        rows = transaction.run(
                            NEIGHBORHOOD_QUERY, {"snapshot_id": snapshot_id, "seeds": seeds},
                        ).data()
                        transaction.commit()
        except self.error_types as error:
            raise SchemaGraphError(
                f"Neo4j retrieval failed ({type(error).__name__}). Check the instance, database and credentials."
            ) from None
        if not rows:
            raise SchemaGraphError(
                "This schema version is not in Neo4j. Run python -m transitmind.schema_graph sync."
            )
        return rows
