"""Run every scenario end to end against OPA and check the outcome.

    uv run python demo.py                         # scripted model: checks every gateway decision exactly
    uv run python demo.py --backend groq          # real model: checks that nothing leaks (see README)

Exits non-zero if a decision differs from the expected one, or if any record is released that the scenario
does not permit, whatever the model did.
"""
import argparse
import json
import sys
import textwrap

from secagent import agent
from secagent.app import AGENT_ID, gateway, scenario_prompt
from secagent.gateway import Session
from secagent.reference import EMPLOYEES, SCENARIOS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="scripted", choices=["scripted", "groq"])
    ap.add_argument("--model")
    ap.add_argument("--only", help="run one scenario id")
    a = ap.parse_args()

    failures = 0
    for sc in SCENARIOS:
        if a.only and sc["id"] != a.only:
            continue
        r = agent.run(gateway, Session(sc["user"], AGENT_ID), scenario_prompt(sc), sc["mode"], a.backend, a.model)
        got = [(c["tool"], c["allow"]) for c in r["calls"]]
        leaked = sorted(set(r["released"]) - set(sc["may_release"]))
        ok = not leaked and (a.backend != "scripted" or got == [tuple(e) for e in sc["expect"]])
        failures += not ok
        print(f"\n{'PASS' if ok else 'FAIL'}  {sc['title']}  [{sc['id']}]")
        print(f"      user={EMPLOYEES[sc['user']]['name']} ({EMPLOYEES[sc['user']]['role']})  instructions={sc['mode']}"
              f"  model={r['backend']}/{r['model']}")
        print(textwrap.indent(textwrap.fill("prompt: " + _short(r["prompt"]), 100), "      "))
        for c in r["calls"]:
            verdict = "ALLOW" if c["allow"] else "DENY  " + ",".join(c["reasons"])
            print(f"      {c['tool']:<22} {json.dumps(_trim(c["args"]), ensure_ascii=False)[:70]:<72} {verdict}")
        print(f"      released: {r['released'] or 'nothing'}" + (f"   LEAKED: {leaked}" if leaked else ""))
        print(textwrap.indent(textwrap.fill("answer: " + r["answer"], 100), "      "))
        if not ok and a.backend == "scripted":
            print(f"      expected {sc['expect']}, got {got}")

    n = len([s for s in SCENARIOS if not a.only or s["id"] == a.only])
    print(f"\n{n - failures}/{n} scenarios passed")
    return 1 if failures else 0


def _short(text: str) -> str:
    return " ".join(t if len(t) < 40 else t[:24] + "…(signed grant)" for t in text.split())


def _trim(args: dict) -> dict:
    return {k: (v[:20] + "…" if isinstance(v, str) and len(v) > 24 else v) for k, v in args.items()}


if __name__ == "__main__":
    sys.exit(main())
