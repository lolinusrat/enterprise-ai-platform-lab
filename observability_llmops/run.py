#!/usr/bin/env python3
"""Ask the support agent one question and print its trace tree and token usage.

    uv run run.py "How much refund for my opened headphones, order A1001?" [--backend groq|ollama|scripted]
"""
import argparse

from obs_agent.graph import build_graph
from obs_agent.runner import run_traced


def print_tree(spans):
    children = {}
    for s in spans:
        children.setdefault(s["parent_id"], []).append(s)

    def walk(parent, depth):
        for s in children.get(parent, []):
            a = s["attributes"]
            extra = ""
            if a.get("gen_ai.operation.name") == "chat":
                extra = (f"  in={a['gen_ai.usage.input_tokens']} out={a['gen_ai.usage.output_tokens']}"
                         f" ${a['gen_ai.usage.cost_usd']:.6f}")
            print(f"{'  ' * depth}{s['name']:<{44 - 2 * depth}} {s['duration_ms']:>8.1f} ms{extra}")
            walk(s["span_id"], depth + 1)

    walk(None, 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--backend", default="groq", choices=["groq", "ollama", "scripted"])
    ap.add_argument("--model")
    ap.add_argument("--prompt", default="v1", choices=["v1", "v2"])
    args = ap.parse_args()
    r = run_traced(build_graph(args.backend, args.model, args.prompt), args.question,
                   {"support.prompt_version": args.prompt})
    print(f"intent: {r['intent']}\nanswer: {r['answer']}\n" + (f"error: {r['error']}\n" if r["error"] else ""))
    print_tree(r["spans"])
    u = r["usage"]
    print(f"\ntokens in={u['input_tokens']} out={u['output_tokens']} (reasoning {u['reasoning_tokens']}) "
          f"total={u['total_tokens']}  cost=${u['cost_usd']:.6f}  llm_calls={u['llm_calls']} "
          f"tool_calls={u['tool_calls']}  latency={u['latency_ms']} ms  trace_id={r['trace_id']}")
