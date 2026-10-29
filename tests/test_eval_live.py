"""Live evaluation suite: one parametrized test per frozen question.

Run all (costs API usage per case):
    TRANSITMIND_LIVE_EVALS=1 pytest -m eval --run-live-evals
Single-case smoke:
    TRANSITMIND_LIVE_EVALS=1 pytest -m eval --run-live-evals -k C1 -s

A wrong model answer is evaluation DATA, not a test failure: these tests only
assert that collection and scoring produced a record; the report aggregates
correctness. pytest is the batch driver, not the judge.
"""

import pytest

from transitmind.evaluation import (
    check_case, collect_case, load_cases, score_payload,
)

pytestmark = pytest.mark.eval
CASES = load_cases()[1]


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_live_case(case, eval_context):
    gold = check_case(case, eval_context["settings"])
    # 行为题可以没有 gold_sql（D1/D2/D4/D6/D9/D10）；可答题必须有可执行的参考结果。
    assert case.expectation != "answerable" or gold.status == "ok", \
        f"{case.id}: gold SQL unusable: {gold.error}"

    payload = collect_case(case, batch=eval_context["batch"])
    record = score_payload(case, payload, gold)
    # 先保存再报错：裁判超时属于评分链路异常，不丢答卷，也不伪装成 Agent 答错。
    eval_context["batch"].save_record(record)
    assert record["scoring_status"] == "scored", f"{case.id}: scoring error: {record['scoring_error']}"
    # 只断言链路完整；模型答错是数据，不是测试失败。
    assert record["judge"]["verdict"] in {"pass", "fail"}
