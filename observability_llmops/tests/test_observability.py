"""Offline tests: run against the scripted backend, so no network or API key is needed."""
import copy
import json
from pathlib import Path

import pytest

from obs_agent.evals import compare, load_dataset, normalize, run_suite, score
from obs_agent.graph import build_graph
from obs_agent.runner import run_traced
from obs_agent.usage import cost_usd, summarize

ROOT = Path(__file__).parent.parent
THRESHOLDS = json.loads((ROOT / "evals/thresholds.json").read_text())


@pytest.fixture(scope="module")
def refund_run():
    return run_traced(build_graph("scripted"), "I want a refund for the fruit box in order A1004.")


def test_span_tree_shape(refund_run):
    spans = refund_run["spans"]
    by_id = {s["span_id"]: s for s in spans}
    roots = [s for s in spans if s["parent_id"] is None]
    assert len(roots) == 1 and roots[0]["name"] == "invoke_agent support_agent"
    assert len({s["trace_id"] for s in spans}) == 1
    for s in spans:
        op = s["attributes"].get("gen_ai.operation.name")
        if op == "chat":
            assert by_id[s["parent_id"]]["name"] in ("node classify", "node agent")
        if op == "execute_tool":
            assert by_id[s["parent_id"]]["name"] == "node tools"
    node_names = [s["attributes"]["langgraph.node"] for s in spans if s["name"].startswith("node ")]
    assert node_names == ["classify", "agent", "tools", "agent", "tools", "agent"]
    assert roots[0]["attributes"]["support.intent"] == "refund"


def test_chat_spans_carry_usage(refund_run):
    chats = [s for s in refund_run["spans"] if s["attributes"].get("gen_ai.operation.name") == "chat"]
    assert chats
    for s in chats:
        a = s["attributes"]
        assert a["gen_ai.request.model"] == "scripted-v1"
        assert a["gen_ai.usage.input_tokens"] > 0 and a["gen_ai.usage.output_tokens"] > 0


def test_usage_totals_match_spans(refund_run):
    u = refund_run["usage"]
    chats = [s["attributes"] for s in refund_run["spans"] if s["attributes"].get("gen_ai.operation.name") == "chat"]
    assert u["input_tokens"] == sum(a["gen_ai.usage.input_tokens"] for a in chats)
    assert u["output_tokens"] == sum(a["gen_ai.usage.output_tokens"] for a in chats)
    assert u["total_tokens"] == u["input_tokens"] + u["output_tokens"]
    assert u["llm_calls"] == len(chats)
    assert u["tools_called"] == ["lookup_order", "get_refund_policy"]
    assert sum(v["llm_calls"] for v in u["by_node"].values()) == u["llm_calls"]


def test_cost_and_reasoning_accounting():
    assert cost_usd("openai/gpt-oss-20b", 1_000_000, 1_000_000) == pytest.approx(0.375)
    assert cost_usd("unpriced-model", 10_000, 10_000) == 0.0
    span = {"status": "UNSET", "duration_ms": 5, "attributes": {
        "gen_ai.operation.name": "chat", "gen_ai.request.model": "openai/gpt-oss-20b", "langgraph.node": "agent",
        "gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 50, "gen_ai.usage.reasoning_tokens": 30,
        "gen_ai.usage.cost_usd": cost_usd("openai/gpt-oss-20b", 100, 50)}}
    u = summarize([span, span])
    assert u["total_tokens"] == 300 and u["reasoning_tokens"] == 60  # reasoning is inside output, not added
    assert u["cost_usd"] == pytest.approx(2 * (100 * 0.075 + 50 * 0.30) / 1e6)


def test_normalize_handles_model_typography():
    assert "2026-09-20" in normalize("delivered on **2026‑09‑20**.")
    assert "can't" in normalize("I can’t approve that")


def test_score_checks():
    case = {"expected_intent": "refund", "required_tools": ["lookup_order", "get_refund_policy"],
            "must_include": [["102"]], "must_not_include": ["full refund"]}
    result = {"answer": "You would get $102.", "intent": "refund", "error": None,
              "usage": {"tools_called": ["lookup_order", "get_refund_policy", "calculate"]}}
    assert all(score(case, result).values())
    result["usage"]["tools_called"] = ["lookup_order"]
    assert score(case, result)["required_tools"] is False


def _report(passed, tokens):
    cases = [{"id": f"c{i}", "passed": p, "failed_checks": [] if p else ["must_include"], "tools_called": ["t"],
              "total_tokens": tokens} for i, p in enumerate(passed)]
    return {"summary": {"pass_rate": 100 * sum(passed) / len(passed), "total_tokens": tokens * len(passed),
                        "cost_usd": 0.001 * tokens, "p95_model_latency_ms": 100}, "cases": cases}


def test_compare_identical_is_ok():
    r = _report([True, True, False], 100)
    assert compare(r, copy.deepcopy(r), THRESHOLDS)["ok"]


def test_compare_flags_case_flip_and_token_growth():
    v = compare(_report([True, True], 100), _report([True, False], 130), THRESHOLDS)
    metrics = {(x["metric"], x.get("case")) for x in v["regressions"]}
    assert not v["ok"]
    assert ("case", "c1") in metrics and ("pass_rate", None) in metrics
    assert ("total_tokens", None) in metrics and ("cost_usd", None) in metrics


def test_prompt_v2_regression_is_caught_end_to_end():
    dataset = load_dataset(ROOT / "evals/dataset.jsonl")
    baseline = run_suite(dataset, "scripted", prompt_version="v1", workers=1)
    current = run_suite(dataset, "scripted", prompt_version="v2", workers=1)
    verdict = compare(baseline, current, THRESHOLDS)
    assert not verdict["ok"]
    flipped = {x["case"] for x in verdict["regressions"] if x["metric"] == "case"}
    assert "status-shipped" in flipped and "policy-electronics" in flipped
    assert compare(baseline, run_suite(dataset, "scripted", prompt_version="v1", workers=1), THRESHOLDS)["ok"]
