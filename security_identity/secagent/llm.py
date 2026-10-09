"""Model backends speaking the OpenAI chat-completions tool-calling format.

groq      - a real model (default openai/gpt-oss-20b) on Groq; needs GROQ_API_KEY.
scripted  - a deterministic stand-in that follows its instructions literally, including bad ones, and obeys
            instructions it finds in tool results. It exists to show that the policy holds even when the
            model does exactly the wrong thing, and to let the demo and tests run offline.
"""
import json
import os
import re
import time

import httpx

from secagent.reference import INSTRUCTION_MODES

TOOLS = [
    {"type": "function", "function": {
        "name": "find_customer",
        "description": "Look up customers by name. Returns ids and names only.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "request_authorization",
        "description": "Ask the authorization server for a short-lived grant to read one customer's record for a "
                       "stated business purpose. Returns a grant token or the reasons it was refused.",
        "parameters": {"type": "object", "properties": {
            "customer_id": {"type": "string"},
            "purpose": {"type": "string", "enum": ["account_servicing", "support_ticket", "fraud_investigation",
                                                   "marketing_outreach"]},
            "reference": {"type": "string", "description": "Ticket (T-...) or case (FC-...) id, if any"}},
            "required": ["customer_id", "purpose"]}}},
    {"type": "function", "function": {
        "name": "get_customer_record",
        "description": "Read a customer's record. Pass the grant returned by request_authorization.",
        "parameters": {"type": "object", "properties": {
            "customer_id": {"type": "string"}, "grant": {"type": "string"}},
            "required": ["customer_id"]}}},
]

GROQ_MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]


def available_backends() -> dict[str, list[str]]:
    out = {"scripted": ["scripted-v1"]}
    if os.environ.get("GROQ_API_KEY"):
        out["groq"] = GROQ_MODELS
    return out


def complete(backend: str, model: str, messages: list[dict]) -> tuple[dict, dict]:
    """Return (assistant message, usage)."""
    if backend == "scripted":
        msg = ScriptedModel().step(messages)
        n_in = sum(len(str(m.get("content") or "")) for m in messages) // 4
        return msg, {"prompt_tokens": n_in, "completion_tokens": len(json.dumps(msg)) // 4}
    if backend == "groq":
        return _groq(model, messages)
    raise ValueError(f"unknown backend {backend!r}")


def _groq(model: str, messages: list[dict]) -> tuple[dict, dict]:
    body = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto", "temperature": 0}
    headers = {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}
    for attempt in range(5):
        r = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=body, headers=headers, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("retry-after", 2 * (attempt + 1))))
            continue
        if r.status_code == 400 and "tool_use_failed" in r.text:
            # The model produced a malformed tool call; ask again rather than fail the run.
            continue
        r.raise_for_status()
        data = r.json()
        m = data["choices"][0]["message"]
        msg = {"role": "assistant", "content": m.get("content") or ""}
        if m.get("tool_calls"):
            msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["function"]["name"],
                                               "arguments": c["function"]["arguments"] or "{}"}}
                                 for c in m["tool_calls"]]
        return msg, data.get("usage", {})
    raise RuntimeError(f"Groq request failed: {r.status_code} {r.text[:200]}")


# --- scripted model --------------------------------------------------------------------------------

ID = re.compile(r"\bC-\d{4}\b")
TOKEN = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
INJECTED = re.compile(r"get_customer_record for (C-\d{4})")


class ScriptedModel:
    def step(self, messages: list[dict]) -> dict:
        system = messages[0]["content"]
        mode = next((k for k, v in INSTRUCTION_MODES.items() if v["prompt"] == system), "compliant")
        question = next(m["content"] for m in messages if m["role"] == "user")
        done = self._history(messages)

        target = (ID.findall(question) or [None])[0]
        found = [r for name, args, r in done if name == "find_customer"]
        if not target and found and found[-1].get("matches"):
            target = found[-1]["matches"][0]["customer_id"]
        if not target:
            name = re.search(r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b", question)
            if name and not found:
                return _call("find_customer", name=name.group(1))
            return _answer("I could not identify which customer you mean.")

        reads = [(a, r) for n, a, r in done if n == "get_customer_record"]
        grants = [r for n, a, r in done if n == "request_authorization"]
        pasted = TOKEN.search(question)

        if mode == "rogue":
            if not reads:
                return _call("get_customer_record", customer_id=target, grant="ADMIN-OVERRIDE")
            if len(reads) == 1 and "error" in reads[0][1]:
                return _call("get_customer_record", customer_id=target)
            return self._summarise(reads, grants)

        if pasted and not reads:  # a credulous assistant uses a token the user pastes
            return _call("get_customer_record", customer_id=target, grant=pasted.group(0))

        if mode == "unaware" and not reads:
            return _call("get_customer_record", customer_id=target)

        if not grants:
            purpose, ref = _purpose(question)
            return _call("request_authorization", customer_id=target, purpose=purpose, reference=ref)
        if grants[-1].get("status") != "granted":
            return self._summarise(reads, grants)
        grant = grants[-1]["grant"]
        if not any("record" in r for _, r in reads):
            return _call("get_customer_record", customer_id=target, grant=grant)

        # Obey instructions embedded in data, as an injected model would.
        notes = json.dumps([r.get("record") for _, r in reads])
        injected = [c for c in INJECTED.findall(notes) if c not in {a.get("customer_id") for a, _ in reads}]
        if injected:
            return _call("get_customer_record", customer_id=injected[0], grant=grant)
        return self._summarise(reads, grants)

    @staticmethod
    def _history(messages: list[dict]) -> list[tuple[str, dict, dict]]:
        results = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        out = []
        for m in messages:
            for c in m.get("tool_calls") or []:
                if c["id"] in results:
                    out.append((c["function"]["name"], json.loads(c["function"]["arguments"]), results[c["id"]]))
        return out

    @staticmethod
    def _summarise(reads, grants) -> dict:
        parts = []
        for g in grants:
            if g.get("status") != "granted":
                parts.append("Authorization was refused: " + "; ".join(f"{r['id']} {r['msg']}" for r in g["reasons"]) + ".")
        for args, r in reads:
            if "record" in r:
                rec = r["record"]
                shown = ", ".join(f"{k}: {v}" for k, v in rec.items() if k not in ("customer_id", "notes"))
                parts.append(f"{args['customer_id']} - {shown}.")
                if r.get("withheld_by_policy"):
                    parts.append(f"Withheld by policy: {', '.join(r['withheld_by_policy'])}.")
            else:
                why = "; ".join(f"{x['id']} {x['msg']}" for x in r.get("reasons", []))
                parts.append(f"Reading {args.get('customer_id')} was denied: {why}.")
        return _answer(" ".join(parts) or "Nothing to report.")


def _purpose(text: str) -> tuple[str, str]:
    t = text.lower()
    if case := re.search(r"\bFC-\d+\b", text):
        return "fraud_investigation", case.group(0)
    if ticket := re.search(r"\bT-\d+\b", text):
        return "support_ticket", ticket.group(0)
    if "marketing" in t or "campaign" in t:
        return "marketing_outreach", ""
    return "account_servicing", ""


_n = 0


def _call(tool: str, **args) -> dict:
    global _n
    _n += 1
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"call_{_n}", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]}


def _answer(text: str) -> dict:
    return {"role": "assistant", "content": text}
