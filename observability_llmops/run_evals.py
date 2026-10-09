#!/usr/bin/env python3
"""Run the eval dataset and gate on regressions against the committed baseline.

    uv run run_evals.py                         # v1 prompt vs evals/baseline.json, exit 1 on regression
    uv run run_evals.py --prompt v2             # the "token-saving" prompt edit; should be caught
    uv run run_evals.py --update-baseline       # accept the current run as the new baseline
    uv run run_evals.py --backend scripted      # offline, deterministic (CI smoke test)
"""
import argparse
import json
import sys
from pathlib import Path

from obs_agent.evals import compare, load_dataset, run_suite, strip_spans

ROOT = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=ROOT / "evals/dataset.jsonl")
    ap.add_argument("--baseline", default=None, help="default: evals/baseline.json (evals/baseline.<backend>.json if not groq)")
    ap.add_argument("--backend", default="groq", choices=["groq", "ollama", "scripted"])
    ap.add_argument("--model")
    ap.add_argument("--prompt", default="v1", choices=["v1", "v2"])
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()

    baseline_path = Path(args.baseline or ROOT / ("evals/baseline.json" if args.backend == "groq"
                                                  else f"evals/baseline.{args.backend}.json"))

    def on_case(row):
        mark = "PASS" if row["passed"] else "FAIL"
        why = f"  [{', '.join(row['failed_checks'])}]" if row["failed_checks"] else ""
        print(f"  {mark}  {row['id']:<28} {row['total_tokens']:>6} tok  {row['latency_ms']:>7.0f} ms  "
              f"tools={row['tools_called']}{why}", flush=True)

    print(f"Running {args.dataset} with backend={args.backend} prompt={args.prompt} repeats={args.repeats}")
    report = run_suite(load_dataset(args.dataset), args.backend, args.model, args.prompt,
                       args.repeats, args.workers, on_case)
    s = report["summary"]
    print(f"\npass {s['passed']}/{s['cases']} ({s['pass_rate']}%)  tokens={s['total_tokens']}  "
          f"cost=${s['cost_usd']:.6f}  model p50={s['p50_model_latency_ms']} ms  p95={s['p95_model_latency_ms']} ms")

    out = ROOT / "evals/results" / (f"{report['meta']['started_at'].replace(':', '')}_{args.backend}_"
                                 f"{report['meta']['model'].split('/')[-1]}_{args.prompt}.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"results: {out.relative_to(ROOT)}")

    if args.update_baseline:
        baseline_path.write_text(json.dumps(strip_spans(report), indent=2, default=str))
        print(f"baseline updated: {baseline_path.relative_to(ROOT)}")
        return 0
    if not baseline_path.exists():
        print(f"no baseline at {baseline_path}; run with --update-baseline first")
        return 0

    thresholds = json.loads((ROOT / "evals/thresholds.json").read_text())
    verdict = compare(json.loads(baseline_path.read_text()), report, thresholds)
    for title, items in (("REGRESSIONS", verdict["regressions"]), ("warnings", verdict["warnings"]),
                         ("improvements", verdict["improvements"])):
        if items:
            print(f"\n{title}:")
            for i in items:
                label = i.get("case") or i["metric"]
                extra = f"  tools {i['baseline_tools']} -> {i['current_tools']}" if "baseline_tools" in i else ""
                print(f"  - {label}: {i['detail']}{extra}")
    print("\nRESULT:", "OK, no regressions" if verdict["ok"] else "REGRESSION DETECTED")
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
