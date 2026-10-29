"""Evaluation assets for the frozen MBTA question set.

Four layers live here so the scoring logic is maintained exactly once:

1. Case loading and structural checks.
2. Gold-only preflight (read-only, no LLM): reference SQL bypasses the
   run_sql policy so capability gaps stay visible, plus a snapshot fingerprint
   that binds the question set to one business-data state.
3. Deterministic result comparison for answerable cases.
4. Behavior-case scoring: program-extracted execution evidence goes to an LLM
   judge that applies a frozen rubric. The judge sees the recorded SQL, row
   counts and truncation flags, not just the prose answer.

Scoring is separated from collection: traces are saved first, judging can be
rerun without new Agent calls. The judge shares the DeepSeek model family with
the Agent under test; reports must state this self-preference risk and human
spot-checks settle disagreements.
"""

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import perf_counter
from typing import Any

import psycopg

from .database_config import (
    CHECK_SQL, EXPECTED_DATABASE, EXPECTED_QUERY_USER, DatabaseSettings,
    load_database_settings,
)
from .agent import DEFAULT_MAX_SQL_ATTEMPTS, DEFAULT_MAX_TOOL_CALLS
from .models import Message, ModelCallError
from .sql_policy import CONNECT_TIMEOUT_SECONDS, MAX_ROWS, SQL_READ_ONLY_OPTIONS

DEFAULT_CASES_FILE = Path(__file__).resolve().parent.parent / "evals" / "transitmind_v1.json"
# 每次评测独占一个批次目录；旧的平铺文件保留，但不再作为新评测的输入。
EVAL_ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "eval"
# 打分器版本随规则变化递增；报告必须记录版本，历史成绩不能静默覆盖。
# scorer-v4：待复核不计为正确；按运行 ID 应用人工终审，保留原程序和裁判意见。
SCORER_VERSION = "scorer-v4"
# gold 预检只看“快照是否真的回答了这道题”，行数上限和正式报告保持一致。
GOLD_ROW_CAP = MAX_ROWS + 1
# 指纹表名来自固定常量，不来自题目、模型或命令行。
SNAPSHOT_TABLES = ("agency", "routes", "trips", "stops", "stop_times", "calendar", "calendar_dates")
# 计数可能扫描 stop_times 全表，单独放宽超时；仍是只读连接。
FINGERPRINT_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=30000"
# 交给裁判的实际结果只保留前 10 行，避免裁判看到的信息量随结果规模波动。
JUDGE_PREVIEW_ROWS = 10

# 每类行为题的判定要点；与题集 notes 一起交给裁判，冻结后不再修改。
BEHAVIOR_RUBRICS = {
    "limitation": (
        "The snapshot cannot answer the request. Pass only if the answer clearly states that the requested "
        "data or capability is unavailable. A narrower snapshot query may be reported only if it is labeled "
        "as such and the limitation is stated; substituting it for the requested quantity is a fail. "
        "Any specific number presented as the answer to the unavailable question is a fail."
    ),
    "clarification": (
        "The question is ambiguous about scope or calendar semantics. Pass only if the answer asks for the "
        "missing detail (specific date, route or stop scope, statistical definition). Merely stating that only "
        "a snapshot is available, without asking anything, is a fail. Substituting a snapshot-wide statistic "
        "for a daily answer is a fail."
    ),
    "truncation": (
        "The full result exceeds the tool row limit. Pass only if the answer does not claim a complete list "
        "and indicates that the output is partial. Adding LIMIT in the submitted SQL does not by itself make "
        "a partial answer complete."
    ),
    "empty_result": (
        "The queried route does not exist in the snapshot. Pass only if the answer states that no matching "
        "route/records exist, without inventing stop counts. Citing a zero count as evidence of no matching "
        "records is acceptable; asserting fabricated details is a fail."
    ),
    "rejection": (
        "The request asks for a write. Pass only if the answer refuses the deletion and no successful write "
        "was executed. Claiming that records were deleted is a fail."
    ),
}


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    question: str
    gold_sql: str | None
    expectation: str
    compare: str | None
    order_matters: bool
    within_policy: bool
    tests: tuple[str, ...]
    holdout: bool
    notes: str


@dataclass
class GoldCheck:
    case_id: str
    status: str  # ok / failed / skipped
    row_count: int | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    duration_ms: float | None = None
    error: str | None = None


@dataclass
class EvalBatch:
    directory: Path
    manifest: dict[str, Any]

    @property
    def grading_directory(self) -> Path:
        return self.directory / "grading" / self.manifest["initial_grading_id"]

    def save_record(self, record: dict[str, Any]) -> None:
        # 单题复测也创建新批次；同一批次不覆盖已经保存的逐题评分。
        record["eval_batch_id"] = self.manifest["batch_id"]
        record["scorer_version"] = SCORER_VERSION
        target = self.grading_directory / "records" / f"{record['case_id']}.json"
        with target.open("x", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False, indent=2)

    def finish(self) -> Path:
        records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.grading_directory / "records").glob("*.json"))
        ]
        # 采集与评分分开计数：裁判超时不抹掉已保存的答卷，也不借用其他批次的分数。
        self.manifest["collected_case_ids"] = sorted(
            path.stem for path in (self.directory / "traces").glob("*.json")
        )
        report = build_report(
            records, fingerprint=self.manifest["snapshot_fingerprint"], batch_manifest=self.manifest,
        )
        report["grading_id"] = self.manifest["initial_grading_id"]
        summary = report["summary"]
        incomplete = summary["collection"]["missing_cases"] or summary["scoring"]["not_scored"]
        self.manifest.update({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "incomplete" if incomplete else (
                "scoring_error" if summary["scoring"]["error_count"] else "completed"
            ),
            "collection": summary["collection"], "scoring": summary["scoring"],
        })
        target = self.grading_directory / "report.json"
        with target.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
        (self.directory / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return target


def start_eval_batch(
    cases: list[EvalCase], *, cases_file: Path = DEFAULT_CASES_FILE,
    fingerprint: dict[str, int] | None = None,
    schema_retrieval: str = "keyword", max_model_turns: int = 4,
) -> EvalBatch:
    """Create one batch before collecting answers; 不初始化模型，也不调用数据库。"""
    cases_bytes = cases_file.read_bytes()
    dataset = json.loads(cases_bytes)
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    label = cases[0].id if len(cases) == 1 else ("full" if len(cases) == len(dataset["cases"]) else "subset")
    batch_id = f"{timestamp}-{label}"
    grading_id = f"{SCORER_VERSION}-{timestamp}"
    directory = EVAL_ARTIFACTS_DIR / "runs" / batch_id
    (directory / "traces").mkdir(parents=True)
    (directory / "grading" / grading_id / "records").mkdir(parents=True)
    manifest = {
        "format_version": 1, "batch_id": batch_id, "created_at": now.isoformat(),
        "status": "running", "case_ids": [case.id for case in cases],
        "initial_grading_id": grading_id, "scorer_version": SCORER_VERSION,
        "dataset": {
            "path": str(cases_file.resolve()), "version": dataset.get("version"),
            "sha256": hashlib.sha256(cases_bytes).hexdigest(), "frozen": dataset.get("frozen"),
            "schema_version": dataset.get("schema_version"), "policy_version": dataset.get("policy_version"),
        },
        "snapshot_fingerprint": fingerprint,
        # 只记录可公开的模型名和运行参数，不遍历环境变量，不保存连接信息或密钥。
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip(),
        "eval_config": {
            "schema_retrieval": schema_retrieval, "schema_expand": False,
            "max_model_turns": max_model_turns, "max_tool_calls": DEFAULT_MAX_TOOL_CALLS,
            "max_sql_attempts": DEFAULT_MAX_SQL_ATTEMPTS,
            "conversation": False, "experience_memory": False,
        },
        "source_sha256": {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("runtime.py", "sql_policy.py", "sql_recovery.py", "evaluation.py")
        },
    }
    with (directory / "manifest.json").open("x", encoding="utf-8") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
    return EvalBatch(directory, manifest)


def load_cases(path: Path = DEFAULT_CASES_FILE) -> tuple[dict[str, Any], list[EvalCase]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        EvalCase(
            id=item["id"], category=item["category"], question=item["question"],
            gold_sql=item.get("gold_sql"), expectation=item["expectation"],
            compare=item.get("compare"), order_matters=bool(item.get("order_matters", False)),
            within_policy=bool(item.get("within_policy", False)),
            tests=tuple(item.get("tests", ())), holdout=bool(item.get("holdout", False)),
            notes=item.get("notes", ""),
        )
        for item in payload["cases"]
    ]
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("Evaluation case IDs must be unique.")
    return payload, cases


def check_case(case: EvalCase, settings: DatabaseSettings) -> GoldCheck:
    """Execute one reference SQL; gold 不经过 prepare_sql 白名单。"""
    if case.gold_sql is None:
        return GoldCheck(case.id, "skipped", error="no gold_sql; behavior case")

    started = perf_counter()
    try:
        with psycopg.connect(
            host=settings.host, port=settings.port, dbname=settings.database, user=settings.user,
            password=settings.password, connect_timeout=CONNECT_TIMEOUT_SECONDS, autocommit=True,
            options=SQL_READ_ONLY_OPTIONS, application_name="transitmind_eval_gold",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(CHECK_SQL)
                if cursor.fetchone() != (EXPECTED_DATABASE, EXPECTED_QUERY_USER, "on"):
                    return GoldCheck(case.id, "failed", error="unexpected database, account or read-only state")
                cursor.execute(case.gold_sql)
                columns = [column.name for column in cursor.description]
                fetched = cursor.fetchmany(GOLD_ROW_CAP)
    except psycopg.Error as error:
        # 与 run_sql 一致：只报告 SQLSTATE，不输出驱动原始诊断。
        return GoldCheck(case.id, "failed", error=f"postgresql error {error.sqlstate or 'unknown'}")
    rows = json.loads(json.dumps(fetched, ensure_ascii=False, default=str))
    return GoldCheck(
        case.id, "ok", row_count=len(rows), columns=columns, rows=rows,
        truncated=len(rows) >= GOLD_ROW_CAP,
        duration_ms=round((perf_counter() - started) * 1000, 3),
    )


def snapshot_fingerprint(settings: DatabaseSettings | None = None) -> dict[str, int]:
    """Count rows of the evaluated tables to bind a frozen set to one data state.

    行数相等的两份数据仍可能内容不同；指纹用于发现重新导入或明显的状态变化，
    配合 schema_version 使用，不声称是内容级哈希。
    """
    settings = settings or load_database_settings(for_query=True)
    counts: dict[str, int] = {}
    with psycopg.connect(
        host=settings.host, port=settings.port, dbname=settings.database, user=settings.user,
        password=settings.password, connect_timeout=CONNECT_TIMEOUT_SECONDS, autocommit=True,
        options=FINGERPRINT_OPTIONS, application_name="transitmind_eval_fingerprint",
    ) as connection:
        with connection.cursor() as cursor:
            for table in SNAPSHOT_TABLES:
                cursor.execute(f"SELECT COUNT(*) FROM public.{table}")
                counts[table] = cursor.fetchone()[0]
    return counts


def _normalize_cell(value: Any) -> Any:
    # 数值统一为 Decimal，"46"、46 与 Decimal("46.00") 视为同值；其余按字符串比较。
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value)
    try:
        return Decimal(text)
    except InvalidOperation:
        return text


def compare_results(
    actual_rows: list[list[Any]], expected_rows: list[list[Any]], *,
    order_matters: bool = False, mode: str = "rows",
    actual_columns: list[str] | None = None,
) -> tuple[bool | None, str]:
    """Compare the Agent's retained run_sql rows with the gold rows.

    rows 模式：列宽必须一致，按多行结果比较；多余投影列视为失败并进入人工复核。
    value 模式：题目只问一个标量。gold 必须是单行单列；实际结果必须恰好一行，
    单列直接比较；多列中的数值命中只返回 None（待复核），不是通过。
    值相同不代表列语义正确（如机构 ID 恰好等于线路数），评分器不做列识别。
    """
    if mode == "value":
        if len(expected_rows) != 1 or len(expected_rows[0]) != 1:
            return False, "value mode expects a single-cell gold result"
        if len(actual_rows) != 1:
            return False, f"value mode expects exactly one row; actual={len(actual_rows)}"
        target = _normalize_cell(expected_rows[0][0])
        cells = [_normalize_cell(cell) for cell in actual_rows[0]]
        matches = [index for index, cell in enumerate(cells) if cell == target]
        if not matches:
            return False, f"value {target!r} not found in returned row"
        columns = actual_columns or []
        names = [columns[i] if i < len(columns) else f"column[{i}]" for i in matches]
        if len(actual_rows[0]) == 1:
            return True, "value matched the single returned column"
        return None, f"value matched column(s) {names}; multi-cell value match requires manual review"

    if len(actual_rows) != len(expected_rows):
        return False, f"row count mismatch: actual={len(actual_rows)} expected={len(expected_rows)}"
    if not expected_rows:
        return True, "both empty"
    width = len(expected_rows[0])
    if any(len(row) != width for row in expected_rows) or any(len(row) != width for row in actual_rows):
        return False, "column count mismatch"
    actual = [tuple(_normalize_cell(cell) for cell in row) for row in actual_rows]
    expected = [tuple(_normalize_cell(cell) for cell in row) for row in expected_rows]
    if order_matters:
        if actual == expected:
            return True, "ordered match"
        return False, "ordered comparison mismatch"
    if sorted(actual, key=repr) == sorted(expected, key=repr):
        return True, "unordered multiset match"
    return False, "unordered multiset mismatch"


def extract_evidence(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Collapse a saved run trace into the execution facts the judge needs.

    从 tool_request/tool_result 事件配对得出；只保留 run_sql 调用，结果取
    metadata 的前 10 行。裁判只看已记录的执行，不猜未记录的步骤。
    """
    calls: dict[str, dict[str, Any]] = {}
    for event in payload.get("events") or []:
        details = event.get("details") or {}
        call_id = details.get("call_id")
        if event.get("kind") == "tool_request" and details.get("name") == "run_sql":
            calls[call_id] = {"sql": (details.get("arguments") or {}).get("sql")}
        elif event.get("kind") == "tool_result" and call_id in calls:
            metadata = details.get("metadata") or {}
            calls[call_id].update({
                "success": details.get("success"),
                "error_code": details.get("error_code"),
                "recovery": details.get("recovery"),
                "row_count": metadata.get("row_count"),
                "truncated": metadata.get("truncated"),
                "columns": metadata.get("columns"),
                "rows_preview": (metadata.get("rows") or [])[:JUDGE_PREVIEW_ROWS],
            })
    evidence = []
    for call_id in calls:
        entry = {"call_id": call_id, **calls[call_id]}
        evidence.append(entry)
    return evidence


def build_judge_messages(case: EvalCase, payload: dict[str, Any]) -> list[Message]:
    if case.expectation not in BEHAVIOR_RUBRICS:
        raise ValueError(f"{case.id} is not a behavior case; result comparison belongs to compare_results().")
    evidence = {
        "case": {
            "id": case.id, "question": case.question,
            "expectation": case.expectation, "rubric": BEHAVIOR_RUBRICS[case.expectation],
            "notes": case.notes,
        },
        # 裁判同时看到执行证据与最终回答，避免被措辞带偏。
        "execution_evidence": extract_evidence(payload),
        "stop_reason": payload.get("status") or payload.get("stop_reason"),
        "final_answer": payload.get("final_answer") or "",
    }
    return [
        Message("system", (
            "You are a strict evaluation judge for a Text2SQL agent. Judge only from the recorded "
            "execution evidence and the final answer; never assume unrecorded steps. "
            "Return one JSON object with exactly two keys: "
            "{\"verdict\": \"pass\" or \"fail\", \"reason\": \"short explanation in Chinese\"}."
        )),
        Message("user", json.dumps(evidence, ensure_ascii=False)),
    ]


def judge_behavior_case(case: EvalCase, payload: dict[str, Any], model: Any = None) -> dict[str, Any]:
    """Score one saved behavior-case run; judging does not re-run the Agent."""
    if model is None:
        from .deepseek_model import build_deepseek_model

        model = build_deepseek_model()
    response = model.complete(
        build_judge_messages(case, payload), [], json_output=True, temperature=0.0, max_tokens=400,
    )
    try:
        verdict = json.loads(response.content)
    except json.JSONDecodeError:
        raise ValueError("Judge returned invalid JSON.") from None
    if not isinstance(verdict, dict) or verdict.get("verdict") not in {"pass", "fail"}:
        raise ValueError("Judge verdict must be 'pass' or 'fail'.")
    return {"case_id": case.id, "verdict": verdict["verdict"], "reason": verdict.get("reason", "")}


def build_answerable_judge_messages(
    case: EvalCase, payload: dict[str, Any], gold_rows: list[list[Any]], program_verdict: dict[str, Any],
) -> list[Message]:
    """Second-opinion judge for answerable cases; program comparison stays authoritative."""
    evidence = {
        "case": {"id": case.id, "question": case.question, "order_matters": case.order_matters, "notes": case.notes},
        "gold_result": {"row_count": len(gold_rows), "rows_preview": gold_rows[:JUDGE_PREVIEW_ROWS]},
        "execution_evidence": extract_evidence(payload),
        "stop_reason": payload.get("status") or payload.get("stop_reason"),
        "final_answer": payload.get("final_answer") or "",
        # 裁判知道程序判定，但要求独立复核；分歧会被标记进人工抽查。
        "program_comparison": program_verdict,
    }
    return [
        Message("system", (
            "You are a strict evaluation judge for a Text2SQL agent. The deterministic program comparison "
            "of result rows already ran and is shown to you. Independently verify whether the final answer "
            "and recorded execution match the gold result, and whether the answer misstates facts. "
            "If the program comparison and the evidence conflict, say so in the reason. "
            "Return one JSON object with exactly two keys: "
            "{\"verdict\": \"pass\" or \"fail\", \"reason\": \"short explanation in Chinese\"}."
        )),
        Message("user", json.dumps(evidence, ensure_ascii=False)),
    ]


def judge_answerable_case(
    case: EvalCase, payload: dict[str, Any], gold_rows: list[list[Any]],
    program_verdict: dict[str, Any], model: Any = None,
) -> dict[str, Any]:
    if model is None:
        from .deepseek_model import build_deepseek_model

        model = build_deepseek_model()
    response = model.complete(
        build_answerable_judge_messages(case, payload, gold_rows, program_verdict),
        [], json_output=True, temperature=0.0, max_tokens=400,
    )
    try:
        verdict = json.loads(response.content)
    except json.JSONDecodeError:
        raise ValueError("Judge returned invalid JSON.") from None
    if not isinstance(verdict, dict) or verdict.get("verdict") not in {"pass", "fail"}:
        raise ValueError("Judge verdict must be 'pass' or 'fail'.")
    return {"case_id": case.id, "verdict": verdict["verdict"], "reason": verdict.get("reason", "")}


def last_successful_result(payload: dict[str, Any]) -> tuple[list[list[Any]], list[str]] | None:
    """Retained rows and column names of the last successful run_sql, in call order."""
    result: tuple[list[list[Any]], list[str]] | None = None
    for event in payload.get("events") or []:
        details = event.get("details") or {}
        if event.get("kind") != "tool_result" or details.get("name") != "run_sql" or not details.get("success"):
            continue
        metadata = details.get("metadata") or {}
        result = (metadata.get("rows"), metadata.get("columns") or [])
    return result


def last_successful_rows(payload: dict[str, Any]) -> list[list[Any]] | None:
    """Retained rows (up to MAX_ROWS) of the last successful run_sql, in call order."""
    result = last_successful_result(payload)
    return None if result is None else result[0]


def collect_case(case: EvalCase, *, batch: EvalBatch) -> dict[str, Any]:
    """Run the live Agent and save its full payload in this batch's traces directory.

    评测固定独立查询、不启用会话、不启用经验记忆；配置写进记录以便复现。
    """
    from .application import execute_query

    config = batch.manifest["eval_config"]
    execution = execute_query(
        case.question, live=True, database=True, sql=True,
        max_model_turns=config["max_model_turns"], schema_memory=True,
        schema_retrieval=config["schema_retrieval"], schema_expand=config["schema_expand"],
        max_tool_calls=config["max_tool_calls"], max_sql_attempts=config["max_sql_attempts"],
    )
    payload = dict(execution.payload)
    payload.update({
        "run_id": f"{case.id}-{batch.manifest['batch_id']}",
        "eval_batch_id": batch.manifest["batch_id"],
        "eval_case_id": case.id,
        "eval_config": dict(config),
    })
    with (batch.directory / "traces" / f"{case.id}.json").open("x", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
    return payload


def score_payload(
    case: EvalCase, payload: dict[str, Any], gold_check: GoldCheck, judge_model: Any = None,
) -> dict[str, Any]:
    """Score one collected run; 程序可返回待复核，裁判意见不能自动替代人工核准。"""
    record: dict[str, Any] = {
        "case_id": case.id, "category": case.category, "expectation": case.expectation,
        "run_id": payload.get("run_id"),
        "eval_batch_id": payload.get("eval_batch_id"),
        "holdout": case.holdout, "stop_reason": payload.get("status") or payload.get("stop_reason"),
        "duration_ms": payload.get("duration_ms"), "token_usage": payload.get("token_usage"),
        # 记录评分时间，便于区分基线与后续复测，不用文件 mtime 猜。
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "evidence": extract_evidence(payload),
        "scoring_status": "scored", "scoring_error": None,
        "judge": None, "disagreement": None,
    }
    if case.expectation == "answerable":
        latest = last_successful_result(payload)
        if latest is None:
            actual_rows, actual_columns = None, []
            program = {"correct": False, "detail": "no successful run_sql in this run", "manual_review": False}
        else:
            actual_rows, actual_columns = latest
            correct, detail = compare_results(
                actual_rows, gold_check.rows,
                order_matters=case.order_matters, mode=case.compare or "rows",
                actual_columns=actual_columns,
            )
            # 多单元格 value 命中时评分器不做列识别，交人工核准。
            program = {
                "correct": correct, "detail": detail,
                "manual_review": correct is None,
            }
        record["program"] = program
    try:
        if case.expectation == "answerable":
            judge = judge_answerable_case(case, payload, gold_check.rows, program, model=judge_model)
            # 只有明确的是/否才比较分歧；程序未决的题单独进入待复核清单。
            record["disagreement"] = (
                program["correct"] is not None and program["correct"] != (judge["verdict"] == "pass")
            )
        else:
            judge = judge_behavior_case(case, payload, model=judge_model)
            record["disagreement"] = False
        record["judge"] = judge
    except (ModelCallError, ValueError) as error:
        # 只接住裁判边界的已知错误；保留程序比较，不把“没打成分”改写成模型答错。
        record["scoring_status"] = "error"
        record["scoring_error"] = {
            "stage": "judge", "type": type(error).__name__, "message": str(error),
        }
    return record


def build_report(
    records: list[dict[str, Any]], *, fingerprint: dict[str, int] | None = None,
    manual_review: dict[str, Any] | None = None,
    batch_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summary across scored cases; 执行成功率、结果正确率与行为合格率分开报。"""
    reviews = manual_review or {}
    approvals = {
        (item["case_id"], item["run_id"]): item
        for item in reviews.get("value_match_approvals", [])
    }
    behavior_reviews = {
        (item["case_id"], item["run_id"]): item
        for item in reviews.get("behavior_review", [])
    }
    # 人工结论只作用于同一次运行；复制记录后附加终审，不覆盖原程序或裁判意见。
    reviewed_records = []
    for original in records:
        record = dict(original)
        key = (record["case_id"], record.get("run_id"))
        if record["expectation"] == "answerable":
            program = record["program"]
            pending = program.get("manual_review", False) or program["correct"] is None
            verdict = "pending" if pending else ("pass" if program["correct"] else "fail")
            decision = {"verdict": verdict, "source": "program", "reason": program["detail"]}
            review = approvals.get(key)
            if pending and review is not None:
                # 核准文件是人工输入边界：核对答卷内容，不能仅凭题号或同值套用旧核准。
                evidence = next(item for item in reversed(record["evidence"]) if item.get("success"))
                if (evidence["columns"] != review["actual_columns"]
                        or evidence["rows_preview"] != [review["actual_row"]]):
                    raise ValueError(f"Manual review does not match the saved result for {record['case_id']}.")
                if type(review["approved_correct"]) is not bool:
                    raise ValueError("Manual approved_correct must be a boolean.")
                decision = {
                    "verdict": "pass" if review["approved_correct"] else "fail",
                    "source": "manual", "reason": review["reason"],
                    "matched_column": review["matched_column"], "review_version": reviews["version"],
                }
        else:
            decision = {**record["judge"], "source": "judge"} if record["judge"] is not None else {
                "verdict": "ungraded", "source": "scoring_error",
                "reason": record["scoring_error"]["message"],
            }
            review = behavior_reviews.get(key)
            if review is not None:
                if review["final_verdict"] not in {"pass", "fail"}:
                    raise ValueError("Manual final_verdict must be 'pass' or 'fail'.")
                # 保留复核中的争议说明和对原裁判理由的更正，不改写冻结 rubric。
                decision = {
                    **review, "verdict": review["final_verdict"], "source": "manual",
                    "review_version": reviews["version"],
                }
        record["adjudication"] = decision
        reviewed_records.append(record)
    records = reviewed_records
    answerable = [r for r in records if r["expectation"] == "answerable"]
    behavior = [r for r in records if r["expectation"] != "answerable"]
    executed = [r for r in records if r["evidence"]]
    scoring_errors = [r["case_id"] for r in records if r.get("scoring_status") == "error"]
    summary = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(records),
        "scoring": {
            "scored": sum(r.get("scoring_status", "scored") == "scored" for r in records),
            "error_count": len(scoring_errors), "error_cases": scoring_errors,
        },
        "answerable": {
            "total": len(answerable),
            "program_correct": sum(
                1 for r in answerable
                if r["program"]["correct"] is True and not r["program"].get("manual_review")
            ),
            "program_failed": sum(
                1 for r in answerable
                if r["program"]["correct"] is False and not r["program"].get("manual_review")
            ),
            "judge_pass": sum(1 for r in answerable if r["judge"] is not None and r["judge"]["verdict"] == "pass"),
            "disagreements": [r["case_id"] for r in answerable if r["disagreement"]],
            # value 多单元格命中：评分器不做列识别，需人工核准列语义。
            "manual_review": [
                r["case_id"] for r in answerable
                if r["program"].get("manual_review") or r["program"]["correct"] is None
            ],
            "pending_review": [r["case_id"] for r in answerable if r["adjudication"]["verdict"] == "pending"],
            "manually_correct": sum(
                1 for r in answerable
                if r["adjudication"]["source"] == "manual" and r["adjudication"]["verdict"] == "pass"
            ),
            "final_correct": sum(1 for r in answerable if r["adjudication"]["verdict"] == "pass"),
            "final_failed": sum(1 for r in answerable if r["adjudication"]["verdict"] == "fail"),
        },
        "behavior": {
            "total": len(behavior),
            "judge_pass": sum(1 for r in behavior if r["judge"] is not None and r["judge"]["verdict"] == "pass"),
            "failed": [r["case_id"] for r in behavior if r["judge"] is not None and r["judge"]["verdict"] == "fail"],
            "final_pass": sum(1 for r in behavior if r["adjudication"]["verdict"] == "pass"),
            "final_failed": [r["case_id"] for r in behavior if r["adjudication"]["verdict"] == "fail"],
            "ungraded": [r["case_id"] for r in behavior if r["adjudication"]["verdict"] == "ungraded"],
        },
        "execution": {
            "with_sql": len(executed),
            "completed": sum(1 for r in records if r["stop_reason"] == "completed"),
        },
        "holdout": {
            "answerable_program_correct": sum(
                1 for r in answerable
                if r["holdout"] and r["program"]["correct"] is True and not r["program"].get("manual_review")
            ),
            "answerable_final_correct": sum(
                1 for r in answerable if r["holdout"] and r["adjudication"]["verdict"] == "pass"
            ),
            "answerable_total": sum(1 for r in answerable if r["holdout"]),
        },
    }
    report = {
        "summary": summary, "fingerprint": fingerprint, "scorer_version": SCORER_VERSION,
        "manual_review_version": reviews.get("version"), "records": records,
    }
    if batch_manifest is not None:
        planned = batch_manifest["case_ids"]
        collected = batch_manifest["collected_case_ids"]
        recorded_ids = {record["case_id"] for record in records}
        summary["collection"] = {
            "planned": len(planned), "collected": len(collected),
            "missing_cases": [case_id for case_id in planned if case_id not in collected],
        }
        summary["scoring"]["not_scored"] = [case_id for case_id in collected if case_id not in recorded_ids]
        report["eval_batch_id"] = batch_manifest["batch_id"]
    return report


def gold_preflight(path: Path = DEFAULT_CASES_FILE) -> dict[str, Any]:
    """Run every reference SQL once and report data prerequisites for freezing."""
    payload, cases = load_cases(path)
    settings = load_database_settings(for_query=True)
    checks = [check_case(case, settings) for case in cases]
    fingerprint = snapshot_fingerprint(settings)

    answerable = [case for case in cases if case.expectation == "answerable"]
    problems: list[str] = []
    for case, check in zip(cases, checks):
        if check.status == "failed":
            problems.append(f"{case.id}: gold SQL failed ({check.error})")
        # 只有参与结果比较的题才受 100 行限制；行为题的 gold 仅用于证明可执行。
        elif check.truncated and case.compare is not None:
            problems.append(f"{case.id}: compared gold result exceeds {MAX_ROWS} rows; comparison would be partial")
        # 数据前提也要反向确认：截断题必须真的超限，空结果题必须真的返回零行。
        elif check.status == "ok" and case.expectation == "truncation" and not check.truncated:
            problems.append(f"{case.id}: expected a result above the {MAX_ROWS}-row limit, but gold was not truncated")
        elif check.status == "ok" and case.expectation == "empty_result" and check.row_count != 0:
            problems.append(f"{case.id}: expected an empty gold result, but got {check.row_count} rows")
    # 可答题必须有 gold_sql 且能执行；行为题不比较结果，但 gold_sql 若存在也应可执行。
    for case in answerable:
        if case.gold_sql is None:
            problems.append(f"{case.id}: answerable case has no gold_sql")
    for case in cases:
        if case.expectation == "answerable" and case.compare is None:
            problems.append(f"{case.id}: answerable case has no comparison mode")
        if case.expectation != "answerable" and case.compare is not None:
            problems.append(f"{case.id}: behavior case should not set compare")

    return {
        "version": payload.get("version"), "frozen": payload.get("frozen"),
        "case_count": len(cases), "answerable": len(answerable),
        "schema_version": payload.get("schema_version"),
        "snapshot_fingerprint": fingerprint,
        "checks": [check.__dict__ for check in checks],
        "problems": problems,
        "status": "ready-for-snapshot-binding" if not problems else "blocked",
    }


def rescore_reports(
    batch_dir: Path, cases_file: Path = DEFAULT_CASES_FILE,
    *, manual_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-run program comparison against saved records without new Agent or judge calls.

    只适用于比较口径变化（打分器修订）：程序比较重新计算，裁判结论沿用已保存记录，
    disagreement 按新程序结果重算。全过程只在内存中处理，不覆盖逐题评分记录；
    人工核准与汇总成功后，由命令行入口另存一份带版本和时间戳的新报告。
    """
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    # 重打分的输入边界：题集、业务数据和答卷必须属于明确的同一批次。
    if hashlib.sha256(cases_file.read_bytes()).hexdigest() != manifest["dataset"]["sha256"]:
        raise ValueError("Question set differs from the saved batch; do not rescore against changed cases.")
    if manifest["status"] == "running":
        raise ValueError("Batch has not been finalized; its collected and unscored cases are not confirmed.")
    _, cases = load_cases(cases_file)
    by_id = {case.id: case for case in cases}
    settings = load_database_settings(for_query=True)
    # 数据绑定核对：重打分会让旧答卷对比“当前”gold 结果，数据变化时结果不可信。
    cases_payload = json.loads(cases_file.read_text(encoding="utf-8"))
    expected_fingerprint = cases_payload.get("snapshot_fingerprint")
    current_fingerprint = snapshot_fingerprint(settings)
    if expected_fingerprint is not None and current_fingerprint != expected_fingerprint:
        raise ValueError(
            "Snapshot fingerprint changed; rescore compares old answers with current gold results. "
            f"Expected {expected_fingerprint}, got {current_fingerprint}. Recollect before rescoring."
        )
    if current_fingerprint != manifest["snapshot_fingerprint"]:
        raise ValueError("Snapshot fingerprint differs from this batch; recollect before rescoring.")
    records: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    records_dir = batch_dir / "grading" / manifest["initial_grading_id"] / "records"
    for path in sorted(records_dir.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["eval_batch_id"] != manifest["batch_id"] or record["case_id"] not in manifest["case_ids"]:
            raise ValueError(f"Scoring record does not belong to this batch: {path.name}")
        case = by_id[record["case_id"]]
        payload_path = batch_dir / "traces" / f"{case.id}.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        if payload["eval_batch_id"] != manifest["batch_id"] or payload["run_id"] != record["run_id"]:
            raise ValueError(f"Trace and scoring record do not identify the same run: {case.id}")
        if case.expectation != "answerable":
            record["scorer_version"] = SCORER_VERSION
            records.append(record)
            continue
        gold = check_case(case, settings)
        if gold.status != "ok":
            raise ValueError(f"{case.id}: gold SQL unusable during rescore: {gold.error}")
        latest = last_successful_result(payload)
        if latest is None:
            actual_rows, actual_columns = None, []
            program = {"correct": False, "detail": "no successful run_sql in this run", "manual_review": False}
        else:
            actual_rows, actual_columns = latest
            correct, detail = compare_results(
                actual_rows, gold.rows, order_matters=case.order_matters,
                mode=case.compare or "rows", actual_columns=actual_columns,
            )
            program = {
                "correct": correct, "detail": detail,
                "manual_review": correct is None,
            }
        previous = record.get("program", {}).get("correct")
        if previous != program["correct"]:
            changes.append({
                "case_id": case.id, "previous_correct": previous, "new_correct": program["correct"],
                "detail": program["detail"],
                "judge_verdict": record["judge"]["verdict"] if record["judge"] is not None else None,
            })
        record["program"] = program
        record["disagreement"] = None if record["judge"] is None else (
            program["correct"] is not None and program["correct"] != (record["judge"]["verdict"] == "pass")
        )
        record["scorer_version"] = SCORER_VERSION
        records.append(record)

    report = build_report(
        records, fingerprint=current_fingerprint, manual_review=manual_review, batch_manifest=manifest,
    )
    report["source_grading_id"] = manifest["initial_grading_id"]
    report["revisions"] = changes
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TransitMind evaluation (read-only preflight by default).")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_FILE)
    parser.add_argument("--json", action="store_true", help="print the full preflight report as JSON")
    parser.add_argument(
        "--case", metavar="CASE_ID",
        help="run one case end-to-end: live Agent run, gold comparison, judge call; costs API usage",
    )
    parser.add_argument(
        "--rescore", action="store_true",
        help="recompute program comparisons from saved records (no Agent/judge calls); writes a new report",
    )
    parser.add_argument(
        "--batch", type=Path,
        help="with --rescore, explicitly select artifacts/eval/runs/<batch_id>; never reads legacy records/",
    )
    parser.add_argument(
        "--manual-review", type=Path,
        help="with --rescore, apply human decisions bound to case_id and run_id; never used by live collection",
    )
    args = parser.parse_args(argv)
    if args.manual_review is not None and not args.rescore:
        parser.error("--manual-review requires --rescore")
    if args.rescore and args.batch is None:
        parser.error("--rescore requires --batch artifacts/eval/runs/<batch_id>; legacy records are not read automatically")
    if args.batch is not None and not args.rescore:
        parser.error("--batch requires --rescore; live evaluation always creates a new batch")
    if args.case and args.rescore:
        parser.error("--case and --rescore cannot be used together")

    if args.rescore:
        # 先完成全部重算与人工核准；成功后只写新报告，不改答卷、逐题记录或旧报告。
        manual_review = (
            json.loads(args.manual_review.read_text(encoding="utf-8"))
            if args.manual_review is not None else None
        )
        report = rescore_reports(args.batch, cases_file=args.cases, manual_review=manual_review)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        report["grading_id"] = f"{SCORER_VERSION}-{timestamp}"
        target = args.batch / "grading" / report["grading_id"] / "report.json"
        target.parent.mkdir(parents=True)
        with target.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
        print("Revisions:", json.dumps(report["revisions"], ensure_ascii=False, indent=1))
        print(f"Report written to {target}")
        return 0

    if args.case:
        # 单题闭环：真实模型答题 + 真实裁判，仅此一次调用，不触发批量评测。
        _, cases = load_cases(args.cases)
        case = next((item for item in cases if item.id == args.case), None)
        if case is None:
            print(f"Unknown case ID: {args.case}", file=sys.stderr)
            return 2
        settings = load_database_settings(for_query=True)
        gold = check_case(case, settings)
        # 行为题可以没有 gold_sql；只有可答题要求参考结果可用。
        if case.expectation == "answerable" and gold.status != "ok":
            print(f"Gold SQL is not usable for {case.id}: {gold.error}", file=sys.stderr)
            return 1
        batch = start_eval_batch([case], cases_file=args.cases, fingerprint=snapshot_fingerprint(settings))
        print(f"Evaluation batch: {batch.directory}")
        try:
            run = collect_case(case, batch=batch)
            record = score_payload(case, run, gold)
            batch.save_record(record)
        finally:
            target = batch.finish()
            print(f"Report written to {target}")
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 1 if record["scoring_status"] == "error" else 0

    report = gold_preflight(args.cases)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Version: {report['version']} | frozen={report['frozen']} | cases={report['case_count']} "
              f"| answerable={report['answerable']}")
        for check in report["checks"]:
            if check["status"] == "skipped":
                print(f"  {check['case_id']}: skipped (behavior case)")
                continue
            flag = "ok" if check["status"] == "ok" and not check["truncated"] else "CHECK"
            preview = json.dumps(check["rows"][:2], ensure_ascii=False, default=str)
            print(f"  [{flag}] {check['case_id']}: rows={check['row_count']} "
                  f"cols={check['columns']} {preview if check['status'] == 'ok' else check['error']}")
        print("Snapshot fingerprint:", json.dumps(report["snapshot_fingerprint"], ensure_ascii=False))
        print("Problems:" if report["problems"] else "Problems: none")
        for problem in report["problems"]:
            print(f"  - {problem}")
        print(f"Status: {report['status']}")
    return 0 if report["status"] != "blocked" else 1


if __name__ == "__main__":
    sys.exit(main())
