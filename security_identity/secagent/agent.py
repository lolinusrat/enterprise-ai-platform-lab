"""The tool-calling agent loop.

The model only proposes tool calls. The runtime executes each one through the tool gateway, attaching the
session's user, agent and session id itself. The model has no credentials and no other route to the data.
"""
import json
import time

from secagent import llm
from secagent.gateway import Session, ToolGateway, Trace
from secagent.reference import INSTRUCTION_MODES

MAX_STEPS = 8


def run(gateway: ToolGateway, session: Session, prompt: str, mode: str, backend: str = "scripted",
        model: str | None = None) -> dict:
    model = model or llm.available_backends()[backend][0]
    trace = Trace()
    messages = [{"role": "system", "content": INSTRUCTION_MODES[mode]["prompt"]}, {"role": "user", "content": prompt}]
    trace.add("user", "agent", "prompt", text=prompt, user=session.user, session=session.id, mode=mode)
    released: list[str] = []
    calls: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    answer = None

    for _ in range(MAX_STEPS):
        trace.add("agent", "llm", "chat completion", backend=backend, model=model,
                  system_prompt=messages[0]["content"], messages=len(messages))
        start = time.perf_counter()
        msg, u = llm.complete(backend, model, messages)
        for k in usage:
            usage[k] += u.get(k, 0) or 0
        ms = round((time.perf_counter() - start) * 1000)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            answer = msg.get("content") or ""
            trace.add("llm", "agent", "final answer", text=answer, latency_ms=ms)
            break
        trace.add("llm", "agent", " + ".join(c["function"]["name"] for c in tool_calls), latency_ms=ms,
                  tool_calls=[{"name": c["function"]["name"], "arguments": _args(c)} for c in tool_calls],
                  text=msg.get("content") or "")
        messages.append(msg)
        for c in tool_calls:
            name, args = c["function"]["name"], _args(c)
            trace.add("agent", "gateway", f"{name}({_brief(args)})", tool=name, arguments=args)
            outcome = gateway.invoke(session, name, args, trace)
            released += outcome["released"]
            calls.append({"tool": name, "args": args, "allow": outcome["allow"],
                          "reasons": list(dict.fromkeys(r["id"] for r in outcome["reasons"]))})
            trace.add("gateway", "agent", "result" if outcome["allow"] else "denied",
                      "allow" if outcome["allow"] else "deny", result=outcome["result"])
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(outcome["result"])})
    if answer is None:
        answer = "Stopped after the maximum number of steps."
        trace.add("llm", "agent", "step limit reached", "deny")
    trace.add("agent", "user", "answer", text=answer)
    return {"session": session.id, "user": session.user, "mode": mode, "backend": backend, "model": model,
            "prompt": prompt, "answer": answer, "calls": calls, "released": sorted(set(released)),
            "usage": usage, "trace": trace.events}


def _args(call: dict) -> dict:
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
        return args if isinstance(args, dict) else {}
    except json.JSONDecodeError:
        return {}


def _brief(args: dict) -> str:
    return ", ".join(f"{k}={(v[:14] + '…') if isinstance(v, str) and len(v) > 18 else v}" for k, v in args.items())
