"""Read saved traces and summarize observed runs without executing the Agent."""

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def record_kind(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("events"), list) and "question" in payload:
        return "run"
    if isinstance(payload.get("matches"), list) and "schema_version" in payload:
        return "schema"
    return None


def read_saved_records(directory: Path) -> Iterator[dict[str, Any]]:
    # 目录仍由工作台传入；兼容 CLI 和网页记录，不读取或改写 data/conversations。
    for path in directory.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except (OSError, ValueError):
            continue
        kind = record_kind(payload)
        if kind is not None:
            yield {
                "id": path.relative_to(directory).as_posix(), "kind": kind,
                "created_at": payload.get("created_at") or modified, "payload": payload,
            }


def summarize_numbers(values: Iterable[int | float | None]) -> dict[str, Any]:
    # 缺失不等于零：均值的分母只包含实际记录了该指标的样本。
    recorded = [value for value in values if type(value) in (int, float)]
    return {
        "total": round(sum(recorded), 3) if recorded else None,
        "mean": round(sum(recorded) / len(recorded), 3) if recorded else None,
        "recorded_count": len(recorded),
    }


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, list] = defaultdict(list)
    tools: dict[str, list] = defaultdict(list)
    stops: Counter = Counter()
    modes: Counter = Counter()
    usages = []
    for run in runs:
        # events 只属于本次运行。不能扫描 messages 计数，否则会把携带的历史再算一次。
        events = run["events"]
        requests = [event["details"] for event in events if event["kind"] == "tool_request"]
        results = {
            event["details"]["call_id"]: event["details"]
            for event in events if event["kind"] == "tool_result"
        }
        budget = run.get("execution_budget") or {}
        metrics["tool_calls"].append(budget.get("tool_call_count", len(requests) if events else None))
        metrics["sql_attempts"].append(budget.get(
            "sql_attempt_count", sum(call["name"] == "run_sql" for call in requests) if events else None,
        ))
        metrics["model_calls"].append(
            sum(event["kind"] == "model_request" for event in events) if events else None,
        )
        for key in ("duration_ms", "model_duration_ms", "tool_duration_ms"):
            metrics[key].append(run.get(key))

        # tool_request 在执行前登记，失败也算尝试；预算或重复请求拦截不产生此事件。
        # 旧 tool_result 没有 name 时，通过本次调用 ID 仍能找到工具名称。
        for request in requests:
            tools[request["name"]].append(results.get(request["call_id"], {}))
        stopped = next((event["details"] for event in reversed(events) if event["kind"] == "run_stopped"), {})
        stops[run.get("stop_reason") or stopped.get("reason") or run.get("status") or "unknown"] += 1
        modes[run.get("mode") or "unknown"] += 1
        if run.get("token_usage") is not None:
            # 公共应用层已经汇总本次 API 用量，这里不再叠加每条消息的 usage。
            usages.append(run["token_usage"])

    tool_rows = []
    for name, results in tools.items():
        tool_rows.append({
            "name": name, "calls": len(results),
            "successes": sum(result.get("success") is True for result in results),
            "failures": sum(result.get("success") is False for result in results),
            "unknown_results": sum(result.get("success") is None for result in results),
            "duration_ms": summarize_numbers(result.get("duration_ms") for result in results),
        })
    count = len(runs)
    complete_usage = sum(
        usage.get("model_calls", 0) > 0 and usage.get("recorded_calls") == usage["model_calls"]
        for usage in usages
    )
    return {
        "run_count": count, "completed_runs": stops["completed"],
        "completion_rate": stops["completed"] / count if count else None,
        "metrics": {key: summarize_numbers(metrics[key]) for key in (
            "tool_calls", "sql_attempts", "model_calls", "duration_ms", "model_duration_ms", "tool_duration_ms",
        )},
        "token_usage": {
            key: summarize_numbers(usage.get(key) for usage in usages)["total"]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "usage_coverage": {
            "recorded_runs": len(usages), "complete_runs": complete_usage,
            "recorded_model_calls": sum(usage.get("recorded_calls", 0) for usage in usages),
        },
        "tools": sorted(tool_rows, key=lambda item: (-item["calls"], item["name"])),
        "stop_reasons": [{"reason": reason, "count": number} for reason, number in stops.most_common()],
        "modes": [{"mode": mode, "count": number} for mode, number in modes.most_common()],
    }


def build_run_statistics(directory: Path) -> dict[str, Any]:
    groups: dict[str, list] = {"live": [], "scripted": [], "unknown": []}
    seen_ids = set()
    schema_count = 0
    duplicate_count = 0
    for record in read_saved_records(directory):
        if record["kind"] != "run":
            schema_count += 1
            continue
        run = record["payload"]
        # 导出的同一份网页 trace 可能又放回 artifacts；有 run_id 时只计一次。
        # 旧 CLI 没有 run_id，保留按文件计数，不按问题文本猜测是否重复。
        run_id = run.get("run_id")
        if run_id:
            if run_id in seen_ids:
                duplicate_count += 1
                continue
            seen_ids.add(run_id)
        mode = run.get("mode") or ""
        group = "live" if mode.startswith("deepseek_live") else "scripted" if mode.startswith("scripted_model") else "unknown"
        groups[group].append(run)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "artifacts/**/*.json", "schema_records_excluded": schema_count,
        "duplicate_runs_excluded": duplicate_count,
        "groups": {name: summarize_runs(runs) for name, runs in groups.items()},
    }
