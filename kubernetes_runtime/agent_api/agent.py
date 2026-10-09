"""A small tool-using agent.

Two backends share the same tools and the same spans:
  groq      - real model through Groq's OpenAI-compatible API (key from the mounted Secret)
  scripted  - deterministic rules, no network; used for load tests, CI and offline demos

`risk_score` is deliberately CPU-bound (PBKDF2), so request volume turns into CPU usage and the
HorizontalPodAutoscaler has something real to react to.
"""
import ast
import hashlib
import json
import operator
import re
import time

from opentelemetry.trace import Status, StatusCode

from .config import GROQ_API_KEY, settings
from .telemetry import LLM_TOKENS, TOOL_CALLS, tracer

ORDERS = {
    "A1001": {"status": "shipped", "carrier": "UPS", "eta": "2026-10-10", "total": 129.00},
    "A1002": {"status": "processing", "carrier": None, "eta": "2026-10-14", "total": 54.50},
    "A1003": {"status": "delivered", "carrier": "FedEx", "eta": "2026-10-02", "total": 980.00},
    "A1004": {"status": "cancelled", "carrier": None, "eta": None, "total": 15.99},
}

SYSTEM_PROMPT = (
    "You are an order-operations assistant. Use the tools to look up orders, score fraud risk "
    "and do arithmetic. Answer in at most three sentences and cite the order id."
)


# ---------------------------------------------------------------- tools

def lookup_order(order_id: str) -> dict:
    order = ORDERS.get(order_id.upper())
    return {"order_id": order_id.upper(), **order} if order else {"error": f"order {order_id} not found"}


def risk_score(order_id: str) -> dict:
    """CPU-bound: PBKDF2 over the order id stands in for feature computation / a local model."""
    digest = hashlib.pbkdf2_hmac("sha256", order_id.upper().encode(), b"risk-v1", settings.risk_iterations)
    score = digest[0] * 100 // 255
    return {"order_id": order_id.upper(), "risk_score": score,
            "band": "high" if score >= 70 else "medium" if score >= 40 else "low"}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.Mod: operator.mod}


def calculate(expression: str) -> dict:
    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError("only arithmetic is allowed")
    try:
        return {"expression": expression, "result": round(ev(ast.parse(expression, mode="eval").body), 4)}
    except (ValueError, SyntaxError, ZeroDivisionError) as e:
        return {"expression": expression, "error": str(e)}


TOOLS = {"lookup_order": lookup_order, "risk_score": risk_score, "calculate": calculate}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "lookup_order", "description": "Status, carrier, ETA and total of an order.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}},
    {"type": "function", "function": {
        "name": "risk_score", "description": "Fraud risk score (0-100) and band for an order.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}},
    {"type": "function", "function": {
        "name": "calculate", "description": "Evaluate an arithmetic expression.",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}},
                       "required": ["expression"]}}},
]


def run_tool(name: str, args: dict) -> dict:
    with tracer.start_as_current_span(f"execute_tool {name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", name)
        TOOL_CALLS.labels(name).inc()
        fn = TOOLS.get(name)
        if fn is None:
            span.set_status(Status(StatusCode.ERROR, "unknown tool"))
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**args)
        except TypeError as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            return {"error": str(e)}


# ---------------------------------------------------------------- backends

def _scripted_plan(message: str) -> list[tuple[str, dict]]:
    text = message.lower()
    ids = [m.upper() for m in re.findall(r"\b[aA]\d{4}\b", message)]
    plan: list[tuple[str, dict]] = []
    for oid in ids:
        if any(w in text for w in ("risk", "fraud", "score")):
            plan.append(("risk_score", {"order_id": oid}))
        if any(w in text for w in ("status", "where", "order", "eta", "ship", "total")) or not plan:
            plan.append(("lookup_order", {"order_id": oid}))
    expr = re.search(r"[\d\.\s\(\)]+[\+\-\*/%][\d\.\s\(\)\+\-\*/%]+", message)
    if expr and not ids:
        plan.append(("calculate", {"expression": expr.group().strip()}))
    return plan


def _scripted_answer(results: list[tuple[str, dict]]) -> str:
    if not results:
        return "I can look up orders (e.g. A1001), score their fraud risk, or do arithmetic."
    parts = []
    for name, r in results:
        if "error" in r:
            parts.append(r["error"] + ".")
        elif name == "lookup_order":
            carrier = f" via {r['carrier']}" if r["carrier"] else ""
            parts.append(f"Order {r['order_id']} is {r['status']}{carrier}, ETA {r['eta']}, total ${r['total']:.2f}.")
        elif name == "risk_score":
            parts.append(f"Order {r['order_id']} has a {r['band']} fraud risk (score {r['risk_score']}).")
        else:
            parts.append(f"{r['expression']} = {r['result']}.")
    return " ".join(parts)


def _run_scripted(message: str, trace_tools: list) -> dict:
    with tracer.start_as_current_span("chat scripted-v1") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", "scripted-v1")
        plan = _scripted_plan(message)
    results = []
    for name, args in plan:
        results.append((name, run_tool(name, args)))
        trace_tools.append(name)
    tokens_in, tokens_out = len(message) // 4 + 40, 30
    LLM_TOKENS.labels("scripted", "input").inc(tokens_in)
    LLM_TOKENS.labels("scripted", "output").inc(tokens_out)
    return {"answer": _scripted_answer(results), "model": "scripted-v1",
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}}


def _run_groq(message: str, trace_tools: list) -> dict:
    from openai import OpenAI

    key = GROQ_API_KEY.get()
    if not key:
        raise RuntimeError("groq backend selected but no GROQ API key is mounted")
    client = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1", max_retries=2, timeout=30)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": message}]
    usage = {"input_tokens": 0, "output_tokens": 0}
    for _ in range(settings.max_agent_steps):
        with tracer.start_as_current_span(f"chat {settings.groq_model}") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.system", "groq")
            span.set_attribute("gen_ai.request.model", settings.groq_model)
            resp = client.chat.completions.create(model=settings.groq_model, messages=messages,
                                                  tools=TOOL_SCHEMAS, temperature=0)
            u = resp.usage
            span.set_attribute("gen_ai.usage.input_tokens", u.prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", u.completion_tokens)
        usage["input_tokens"] += u.prompt_tokens
        usage["output_tokens"] += u.completion_tokens
        LLM_TOKENS.labels("groq", "input").inc(u.prompt_tokens)
        LLM_TOKENS.labels("groq", "output").inc(u.completion_tokens)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return {"answer": msg.content, "model": settings.groq_model, "usage": usage}
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            result = run_tool(tc.function.name, json.loads(tc.function.arguments or "{}"))
            trace_tools.append(tc.function.name)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
    return {"answer": "Stopped after the step limit.", "model": settings.groq_model, "usage": usage}


def run_agent(message: str, backend: str) -> dict:
    tools: list[str] = []
    started = time.perf_counter()
    with tracer.start_as_current_span("invoke_agent order-ops") as span:
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("agent.backend", backend)
        out = _run_groq(message, tools) if backend == "groq" else _run_scripted(message, tools)
        span.set_attribute("agent.tools", ",".join(tools))
        trace_id = format(span.get_span_context().trace_id, "032x")
    return {**out, "backend": backend, "tools": tools, "trace_id": trace_id,
            "agent_ms": round((time.perf_counter() - started) * 1000, 1)}
