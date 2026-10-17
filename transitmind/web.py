"""A local workbench that reuses the command-line Agent and saved traces."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import DEFAULT_MAX_SQL_ATTEMPTS, DEFAULT_MAX_TOOL_CALLS
from .application import execute_query


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIRECTORY = Path(__file__).resolve().parent / "static"
ARTIFACTS_DIRECTORY = PROJECT_ROOT / "artifacts"
app = FastAPI(title="TransitMind local workbench", version="0.0.1")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
app.mount("/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")


class RunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    schema_retrieval: Literal["off", "keyword", "vector", "graph", "hybrid"] = "keyword"
    schema_expand: bool = False
    max_model_turns: int = Field(default=4, ge=1, le=12)
    max_tool_calls: int = Field(default=DEFAULT_MAX_TOOL_CALLS, ge=0)
    max_sql_attempts: int = Field(default=DEFAULT_MAX_SQL_ATTEMPTS, ge=0)


class SchemaEditRequest(BaseModel):
    # 只接收说明类字段，不接收列类型、主外键或数据库连接信息。
    model_config = {"extra": "forbid"}
    expected_version: str = Field(min_length=1, max_length=64)
    description: str = Field(max_length=2000)
    keywords: list[Annotated[str, Field(max_length=80)]] = Field(max_length=30)
    column_descriptions: dict[str, Annotated[str, Field(max_length=1000)]] = Field(max_length=200)


class SchemaEnrichRequest(BaseModel):
    model_config = {"extra": "forbid"}
    expected_version: str = Field(min_length=1, max_length=64)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIRECTORY / "index.html")


@app.get("/api/config")
def configuration() -> dict[str, Any]:
    # 只检查本地快照，不连接数据库，也不为了打开页面调用模型。
    from .schema_store import JsonSchemaStore

    snapshot = None
    try:
        saved = JsonSchemaStore().load()
        snapshot = {
            "database_id": saved["database_id"], "version": saved["version"],
            "table_count": len(saved["tables"]),
        }
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {
        "name": "TransitMind", "database": "transitmind", "schema_snapshot": snapshot,
        "execution_budget_defaults": {
            "max_tool_calls": DEFAULT_MAX_TOOL_CALLS, "max_sql_attempts": DEFAULT_MAX_SQL_ATTEMPTS,
        },
    }


@app.get("/api/schema")
def get_schema_snapshot() -> dict[str, Any]:
    from .schema_store import JsonSchemaStore

    # 管理页读取与 schema_retrieve 相同的本地快照，不运行 Agent 或重新采集数据库结构。
    try:
        return JsonSchemaStore().load()
    except (OSError, ValueError):
        raise HTTPException(status_code=409, detail="无法读取 Schema 快照，请先完成命令行 Schema 同步。") from None


@app.patch("/api/schema/tables/{table_name}")
def edit_schema_table(table_name: str, request: SchemaEditRequest) -> dict[str, Any]:
    from .schema_management import SchemaVersionConflict, update_table_annotations
    from .schema_store import JsonSchemaStore

    try:
        return update_table_annotations(JsonSchemaStore(), table_name, **request.model_dump())
    except SchemaVersionConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema 中没有这张表。") from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except OSError:
        raise HTTPException(status_code=500, detail="Schema 保存失败，请检查本地目录写入权限。") from None


@app.get("/api/schema/status")
def schema_index_status() -> dict[str, Any]:
    from .schema_status import get_schema_status

    try:
        return get_schema_status()
    except (OSError, ValueError, KeyError):
        raise HTTPException(status_code=409, detail="无法读取当前 Schema 版本。") from None


@app.post("/api/schema/tables/{table_name}/enrich")
def enrich_schema_table(table_name: str, request: SchemaEnrichRequest) -> dict[str, Any]:
    from .deepseek_model import ModelCallError
    from .schema_enrichment import generate_schema_suggestion
    from .schema_management import SchemaVersionConflict
    from .schema_store import JsonSchemaStore

    try:
        return generate_schema_suggestion(JsonSchemaStore(), table_name, expected_version=request.expected_version)
    except SchemaVersionConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema 中没有这张表。") from None
    except ModelCallError as error:
        raise HTTPException(status_code=502, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except OSError:
        raise HTTPException(status_code=409, detail="无法读取本地 Schema 快照。") from None


def record_kind(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("events"), list) and "question" in payload:
        return "run"
    if isinstance(payload.get("matches"), list) and "schema_version" in payload:
        return "schema"
    return None


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    records = []
    # 同时读取旧 CLI 的 artifacts/*.json 和网页新保存的 artifacts/runs/*.json。
    paths = sorted(ARTIFACTS_DIRECTORY.rglob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        kind = record_kind(payload)
        if kind is None:
            continue
        question = payload.get("question") or "Schema · " + ", ".join(payload.get("seed_tables", []))
        records.append({
            "id": path.relative_to(ARTIFACTS_DIRECTORY).as_posix(), "kind": kind,
            "question": question, "status": payload.get("status", payload.get("stop_reason", "loaded")),
            "created_at": payload.get("created_at") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "duration_ms": payload.get("duration_ms"),
        })
        if len(records) == 50:
            break
    return records


@app.get("/api/runs/{record_id:path}")
def get_run(record_id: str) -> dict[str, Any]:
    # 记录名来自运行列表；路径必须留在 artifacts 内，不能用此接口读取 .env。
    target = (ARTIFACTS_DIRECTORY / record_id).resolve()
    if not target.is_relative_to(ARTIFACTS_DIRECTORY.resolve()) or target.suffix != ".json":
        raise HTTPException(status_code=404, detail="Run record not found.")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Run record not found.") from None
    if record_kind(payload) is None:
        raise HTTPException(status_code=404, detail="This file is not an Agent or schema record.")
    return payload

# @app.post("/api/runs") 登记规则：向这个地址提交数据时，执行下面的 run_query() 函数。
# request: RunRequest 告诉 FastAPI：传来的数据，要按照我们定义的 RunRequest 结构接收。
@app.post("/api/runs")
def run_query(request: RunRequest) -> dict[str, Any]:
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="请输入问题。")
    if request.schema_expand and request.schema_retrieval == "off":
        raise HTTPException(status_code=422, detail="开启定向扩展前，请选择一种 Schema 检索方式。")

    started = perf_counter()
    run_id = uuid4().hex
    schema_enabled = request.schema_retrieval != "off"
    schema_config = {
        "enabled": schema_enabled, "retrieval_mode": request.schema_retrieval,
        "expansion_enabled": request.schema_expand,
    }
    payload: dict[str, Any] = {
        "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "question": question, "mode": "deepseek_live_postgres_sql",
        "config": request.model_dump(exclude={"question"}),
        "schema_memory": schema_config,
        "messages": [], "events": [], "final_answer": None,
    }
    try:
        # FastAPI 会在线程池中执行这个同步接口；Agent 内部仍是原来的反馈循环。
        execution = execute_query(
            question, live=True, database=True, sql=True, max_model_turns=request.max_model_turns,
            schema_memory=schema_enabled,
            schema_retrieval=request.schema_retrieval if schema_enabled else "keyword",
            schema_expand=request.schema_expand,
            max_tool_calls=request.max_tool_calls, max_sql_attempts=request.max_sql_attempts,
        )
        # 历史和经验不传参时保持关闭；应用层负责上下文、Agent 调用和客户端释放。
        payload.update(execution.payload)
        # 保留网页原有 Schema 配置字段形状，CLI 的 store_path 仍保存在 CLI trace 中。
        payload["schema_memory"] = schema_config
        # 为什么还要有 payload？ 因为除了 Agent 的结果，网页还需要问题、运行编号、创建时间、检索设置等信息。
        # payload 把它们集中到一起，作为这次运行的完整记录。
        payload["status"] = execution.result.stop_reason
    except (ImportError, ValueError, RuntimeError, OSError, TypeError, KeyError) as error:
        # 连接与模型适配层已经清理了敏感诊断；失败记录也保留，便于页面回看。
        # 初始化等尚无公共运行结果的异常仍只记录总耗时；模型错误已由应用层带回已有 trace。
        payload.update({
            "status": "error", "error": str(error),
            "duration_ms": round((perf_counter() - started) * 1000, 3),
        })
    payload["record_id"] = f"runs/{run_id}.json"
    target = ARTIFACTS_DIRECTORY / payload["record_id"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Start the local TransitMind workbench.")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    # 网页只在本机地址监听；打开页面不会自动发起任何业务查询。
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
