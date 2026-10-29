"""Judge one saved legacy behavior answer without running the Agent or SQL."""

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from transitmind.deepseek_model import build_deepseek_model
from transitmind.evaluation import (
    BEHAVIOR_RUBRICS, DEFAULT_CASES_FILE, EVAL_ARTIFACTS_DIR, SCORER_VERSION,
    GoldCheck, build_judge_messages, build_report, load_cases, score_payload,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = EVAL_ARTIFACTS_DIR.resolve()
    trace_path = args.trace.resolve()
    report_path = args.source_report.resolve()
    output_dir = args.output_dir.resolve()
    if not all(path.is_relative_to(root) for path in (trace_path, report_path, output_dir)):
        parser.error("Trace, report and output must stay inside this project's artifacts/eval directory.")

    trace_bytes = trace_path.read_bytes()
    source_bytes = report_path.read_bytes()
    trace = json.loads(trace_bytes)
    source = json.loads(source_bytes)
    dataset, cases = load_cases()
    case = next(item for item in cases if item.id == trace["eval_case_id"])
    if case.expectation not in BEHAVIOR_RUBRICS or trace["question"] != case.question:
        parser.error("The saved trace must match a frozen behavior question; answerable cases need gold results.")
    ids = [record["case_id"] for record in source["records"]]
    if (case.id in ids or len(set(ids)) != len(ids)
            or set(ids) | {case.id} != {item.id for item in cases}):
        parser.error("The source report must contain every other case exactly once and omit this case.")
    if source["fingerprint"] != dataset["snapshot_fingerprint"]:
        parser.error("The saved report and question set have different recorded data fingerprints.")

    # 请求证据先落盘；只复用冻结 rubric，不重新采集答卷，也不重算 gold。
    output_dir.mkdir(parents=True)
    messages = build_judge_messages(case, trace)
    request = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_id": case.id, "run_id": trace["run_id"],
        "source_trace": str(trace_path), "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
        "source_report": str(report_path), "report_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "cases_sha256": hashlib.sha256(DEFAULT_CASES_FILE.read_bytes()).hexdigest(),
        "messages": [asdict(message) for message in messages],
        "agent_rerun": False, "database_requeried": False,
    }
    with (output_dir / "judge-request.json").open("x", encoding="utf-8") as output:
        json.dump(request, output, ensure_ascii=False, indent=2)

    model = build_deepseek_model()
    started = perf_counter()
    try:
        # 行为题不需要 gold SQL。已知裁判异常由 score_payload 留档，不自动重试。
        record = score_payload(case, trace, GoldCheck(case.id, "skipped"), judge_model=model)
    finally:
        model.client.close()
    record.update({
        "scorer_version": SCORER_VERSION, "judge_model": model.model,
        "judge_duration_ms": round((perf_counter() - started) * 1000, 3),
        "grading_source": "llm_judge_supplement", "agent_rerun": False,
    })
    report = build_report([*source["records"], record], fingerprint=source["fingerprint"])
    report["supplement"] = {
        "case_id": case.id, "run_id": trace["run_id"], "reused_records": len(ids),
        "source_report_sha256": request["report_sha256"],
        "source_trace_sha256": request["trace_sha256"],
        "request_file": "judge-request.json", "agent_rerun": False, "database_requeried": False,
        # 原报告仍保留 29 题；新报告明确补评来源，不把失败的第一次裁判尝试抹掉。
        "previous_attempt": {
            "stage": "judge", "status": "error", "type": "ModelCallError",
            "message": "DeepSeek request timed out. Check the network before retrying.",
            "evidence": "User-provided pytest output for test_live_case[D10], 2026-10-29.",
        },
    }
    for name, value in (("record.json", record), ("report.json", report)):
        with (output_dir / name).open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
    print(json.dumps({
        "case_id": case.id, "run_id": trace["run_id"],
        "scoring_status": record["scoring_status"], "judge": record["judge"],
        "scoring_error": record["scoring_error"], "output_dir": str(output_dir),
        "summary": report["summary"],
    }, ensure_ascii=False, indent=2))
    return 1 if record["scoring_status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
