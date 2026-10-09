"""Token and cost accounting, computed from captured spans (the spans are the source of truth)."""
from collections import defaultdict

# USD per 1M tokens (input, output), Groq on-demand list prices; check groq.com/pricing before relying on them.
# Unlisted models (Ollama, scripted) cost 0. Reasoning tokens sit inside output tokens, so they are not billed twice.
PRICES = {
    "openai/gpt-oss-20b": (0.075, 0.30),
    "openai/gpt-oss-120b": (0.15, 0.60),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    return (input_tokens * p_in + output_tokens * p_out) / 1_000_000


def summarize(spans: list[dict]) -> dict:
    """Aggregate token usage, cost, call counts and latency for one or more traces.

    `spans` are dicts in the JSONL exporter's shape (see telemetry.span_to_dict)."""
    total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0,
             "cost_usd": 0.0, "llm_calls": 0, "tool_calls": 0, "errors": 0}
    by_node = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "llm_calls": 0})
    by_model = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "llm_calls": 0})
    tools = []
    root_ms = model_ms = 0.0
    for s in spans:
        a = s["attributes"]
        op = a.get("gen_ai.operation.name")
        if s["status"] == "ERROR":
            total["errors"] += 1
        if op == "invoke_agent":
            root_ms += s["duration_ms"]
        elif op == "chat":
            i, o = a.get("gen_ai.usage.input_tokens", 0), a.get("gen_ai.usage.output_tokens", 0)
            c = a.get("gen_ai.usage.cost_usd", 0.0)
            total["input_tokens"] += i
            total["output_tokens"] += o
            total["reasoning_tokens"] += a.get("gen_ai.usage.reasoning_tokens", 0)
            total["cost_usd"] += c
            total["llm_calls"] += 1
            model_ms += s["duration_ms"]
            for bucket in (by_node[a.get("langgraph.node", "?")], by_model[a.get("gen_ai.request.model", "?")]):
                bucket["input_tokens"] += i
                bucket["output_tokens"] += o
                bucket["cost_usd"] += c
                bucket["llm_calls"] += 1
        elif op == "execute_tool":
            total["tool_calls"] += 1
            tools.append(a.get("gen_ai.tool.name"))
    total["total_tokens"] = total["input_tokens"] + total["output_tokens"]
    return {**total, "latency_ms": round(root_ms, 1), "model_latency_ms": round(model_ms, 1), "tools_called": tools,
            "by_node": dict(by_node), "by_model": dict(by_model)}
