"""Root-level pytest configuration: live evaluation is double-gated.

Everyday `pytest` (addopts in pyproject.toml excludes the eval marker) only runs
the free offline suites. A live evaluation requires BOTH the eval marker AND
--run-live-evals plus TRANSITMIND_LIVE_EVALS=1, because each case is one real
DeepSeek answer plus at least one judge call.
"""

import os
import warnings

import pytest

from transitmind.database_config import load_database_settings
from transitmind.evaluation import gold_preflight, load_cases, start_eval_batch


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-evals", action="store_true", default=False,
        help="allow live LLM evaluation runs (costs API usage)",
    )


@pytest.fixture(scope="session")
def eval_context(request):
    # 第一道闸门：显式开关 + 环境变量，缺一个就跳过而不是失败。
    if not request.config.getoption("--run-live-evals") or os.environ.get("TRANSITMIND_LIVE_EVALS") != "1":
        pytest.skip("Live evaluation requires --run-live-evals and TRANSITMIND_LIVE_EVALS=1.")
    # 预检阻塞时先修题集，不拿无效考卷花钱。
    preflight = gold_preflight()
    if preflight["status"] == "blocked":
        pytest.fail("Gold preflight blocked: " + "; ".join(preflight["problems"]))
    if preflight["frozen"] is not True:
        warnings.warn("Question set is not frozen; results are smoke tests only, not a baseline.")
    _, cases = load_cases()
    # session.items 已经过 -k 等条件筛选；清单只列本次真正准备运行的题。
    selected_cases = [
        item.callspec.params["case"] for item in request.session.items
        if item.get_closest_marker("eval") is not None
    ]
    batch = start_eval_batch(selected_cases, fingerprint=preflight["snapshot_fingerprint"])
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    write_line = reporter.write_line if reporter is not None else print
    write_line(f"Evaluation batch: {batch.directory}")
    try:
        yield {
            "cases": cases, "batch": batch,
            "settings": load_database_settings(for_query=True),
            "fingerprint": preflight["snapshot_fingerprint"],
            "frozen": preflight["frozen"] is True,
        }
    finally:
        # 单题失败仍汇总本批次；没有采集或评分的题明确列出，不读取旧 records/ 补齐。
        target = batch.finish()
        write_line(f"Evaluation report written to {target}")
