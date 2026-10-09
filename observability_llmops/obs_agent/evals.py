"""Evaluation harness: run a dataset through the instrumented graph, score each case, compare to a baseline.

A case passes when every check passes:
  intent          classifier output equals expected_intent
  required_tools  every listed tool was called (as a multiset, read from the execute_tool spans)
  forbidden_tools none of these were called
  must_include    each group is a list of alternatives; at least one per group appears (case-insensitive)
  must_not_include none of these strings appear
  no_error        the run did not raise
"""
import json
import re
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .graph import build_graph
from .models import DEFAULT_MODELS
from .runner import run_traced
from .telemetry import setup_tracing


def load_dataset(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-")
_SPACES = dict.fromkeys(map(ord, "\u00a0\u202f\u2009"), " ")


def normalize(text: str) -> str:
    """Lower-case, map unicode dashes/spaces and curly quotes to ASCII, drop markdown emphasis."""
    text = text.translate(_DASHES).translate(_SPACES).replace("\u2019", "'")
    return re.sub(r"[*_`]", "", text).lower()


def score(case: dict, result: dict) -> dict:
    answer = normalize(result["answer"])
    called = Counter(result["usage"]["tools_called"])
    checks = {"no_error": result["error"] is None}
    if "expected_intent" in case:
        checks["intent"] = result["intent"] == case["expected_intent"]
    if case.get("required_tools"):
        checks["required_tools"] = not (Counter(case["required_tools"]) - called)
    if case.get("forbidden_tools"):
        checks["forbidden_tools"] = not any(called[t] for t in case["forbidden_tools"])
    if case.get("must_include"):
        checks["must_include"] = all(any(normalize(alt) in answer for alt in group) for group in case["must_include"])
    if case.get("must_not_include"):
        checks["must_not_include"] = not any(normalize(s) in answer for s in case["must_not_include"])
    return checks


def _pct(values, q):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, round(q * (len(values) - 1)))]


def run_suite(dataset: list[dict], backend="groq", model=None, prompt_version="v1",
              repeats=1, workers=4, on_case=None) -> dict:
    setup_tracing()
    graph = build_graph(backend, model, prompt_version)
    started = time.time()

    def one(case):
        runs = []
        for r in range(repeats):
            res = run_traced(graph, case["question"], {"eval.case_id": case["id"], "eval.repeat": r,
                                                       "support.prompt_version": prompt_version})
            res["checks"] = score(case, res)
            res["passed"] = all(res["checks"].values())
            runs.append(res)
        n_pass = sum(r["passed"] for r in runs)
        failed = sorted({k for r in runs for k, ok in r["checks"].items() if not ok})
        usage = [r["usage"] for r in runs]
        row = {
            "id": case["id"], "tags": case.get("tags", []), "question": case["question"],
            "passed": n_pass * 2 > repeats, "pass_fraction": n_pass / repeats, "failed_checks": failed,
            "answer": runs[-1]["answer"], "intent": runs[-1]["intent"], "error": runs[-1]["error"],
            "trace_id": runs[-1]["trace_id"], "tools_called": runs[-1]["usage"]["tools_called"],
            "total_tokens": round(statistics.mean(u["total_tokens"] for u in usage)),
            "input_tokens": round(statistics.mean(u["input_tokens"] for u in usage)),
            "output_tokens": round(statistics.mean(u["output_tokens"] for u in usage)),
            "cost_usd": statistics.mean(u["cost_usd"] for u in usage),
            "latency_ms": statistics.mean(u["latency_ms"] for u in usage),
            "model_latency_ms": statistics.mean(u["model_latency_ms"] for u in usage),
            "llm_calls": statistics.mean(u["llm_calls"] for u in usage),
            "tool_calls": statistics.mean(u["tool_calls"] for u in usage),
            "spans": runs[-1]["spans"],
        }
        if on_case:
            on_case(row)
        return row

    with ThreadPoolExecutor(max_workers=workers) as pool:
        cases = list(pool.map(one, dataset))

    latencies = [c["model_latency_ms"] for c in cases]
    tags = sorted({t for c in cases for t in c["tags"]})
    summary = {
        "cases": len(cases),
        "passed": sum(c["passed"] for c in cases),
        "pass_rate": round(100 * sum(c["passed"] for c in cases) / len(cases), 1),
        "total_tokens": sum(c["total_tokens"] for c in cases),
        "input_tokens": sum(c["input_tokens"] for c in cases),
        "output_tokens": sum(c["output_tokens"] for c in cases),
        "cost_usd": round(sum(c["cost_usd"] for c in cases), 6),
        "llm_calls": sum(c["llm_calls"] for c in cases),
        "tool_calls": sum(c["tool_calls"] for c in cases),
        "p50_model_latency_ms": round(_pct(latencies, 0.5), 1),
        "p95_model_latency_ms": round(_pct(latencies, 0.95), 1),
        "by_tag": {t: round(100 * statistics.mean(c["passed"] for c in cases if t in c["tags"]), 1) for t in tags},
    }
    return {
        "meta": {"backend": backend, "model": model or DEFAULT_MODELS[backend], "prompt_version": prompt_version,
                 "repeats": repeats, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
                 "wall_s": round(time.time() - started, 1)},
        "summary": summary,
        "cases": cases,
    }


def _delta_pct(new, old):
    return 0.0 if old == 0 else 100 * (new - old) / old


def compare(baseline: dict, current: dict, thresholds: dict) -> dict:
    """Return regressions (gate failures), warnings and improvements of `current` against `baseline`."""
    b, c = baseline["summary"], current["summary"]
    regressions, warnings, improvements = [], [], []

    drop = b["pass_rate"] - c["pass_rate"]
    if drop > thresholds["pass_rate_drop_pp"]:
        regressions.append({"metric": "pass_rate", "baseline": b["pass_rate"], "current": c["pass_rate"],
                            "detail": f"pass rate fell {drop:.1f} pp"})
    elif drop < 0:
        improvements.append({"metric": "pass_rate", "baseline": b["pass_rate"], "current": c["pass_rate"],
                             "detail": f"pass rate rose {-drop:.1f} pp"})

    for metric, key in (("total_tokens", "total_tokens_increase_pct"), ("cost_usd", "cost_increase_pct")):
        d = _delta_pct(c[metric], b[metric])
        item = {"metric": metric, "baseline": b[metric], "current": c[metric], "detail": f"{d:+.1f}%"}
        if d > thresholds[key]:
            regressions.append(item)
        elif d < -thresholds[key]:
            improvements.append(item)

    d = _delta_pct(c["p95_model_latency_ms"], b["p95_model_latency_ms"])
    if d > thresholds["p95_model_latency_increase_pct"]:
        warnings.append({"metric": "p95_model_latency_ms", "baseline": b["p95_model_latency_ms"],
                         "current": c["p95_model_latency_ms"], "detail": f"{d:+.1f}%"})

    base_cases = {x["id"]: x for x in baseline["cases"]}
    for case in current["cases"]:
        old = base_cases.get(case["id"])
        if old is None:
            continue
        if old["passed"] and not case["passed"]:
            regressions.append({"metric": "case", "case": case["id"],
                                "detail": f"pass -> fail ({', '.join(case['failed_checks'])})",
                                "baseline_tools": old["tools_called"], "current_tools": case["tools_called"]})
        elif not old["passed"] and case["passed"]:
            improvements.append({"metric": "case", "case": case["id"], "detail": "fail -> pass"})
        elif old["tools_called"] != case["tools_called"]:
            warnings.append({"metric": "trajectory", "case": case["id"],
                             "detail": f"{old['tools_called']} -> {case['tools_called']}"})
        d = _delta_pct(case["total_tokens"], old["total_tokens"])
        if d > thresholds["case_tokens_increase_pct"]:
            warnings.append({"metric": "case_tokens", "case": case["id"],
                             "detail": f"{old['total_tokens']} -> {case['total_tokens']} tokens ({d:+.0f}%)"})
    return {"ok": not regressions, "regressions": regressions, "warnings": warnings, "improvements": improvements}


def strip_spans(report: dict) -> dict:
    """Baselines keep the scores and usage, not the full span payload."""
    return {**report, "cases": [{k: v for k, v in c.items() if k != "spans"} for c in report["cases"]]}
