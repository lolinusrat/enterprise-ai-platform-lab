"""Compare vector-only, BM25, hybrid and reranked retrieval, and three ways of enforcing permissions.

    uv run run_evals.py                 # prints the report and writes results/eval_report.json
    uv run run_evals.py --query q35     # show each configuration's ranking for one query
    uv run run_evals.py --rerankers Xenova/ms-marco-MiniLM-L-6-v2 BAAI/bge-reranker-base
                                        # compare cross-encoders, writes results/reranker_comparison.json
"""
import argparse
import json
from pathlib import Path

from hybrid_rag.evals import QUALITY_METRICS, load_queries, run_all, run_config
from hybrid_rag.pipeline import RetrievalPipeline
from hybrid_rag.rerank import Reranker

OUT = Path(__file__).resolve().parent / "results" / "eval_report.json"


def table(rows: list[list], header: list[str]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    fmt = lambda r: "  ".join(str(x).ljust(w) for x, w in zip(r, widths))
    return "\n".join([fmt(header), fmt(["-" * w for w in widths]), *map(fmt, rows)])


def print_report(report: dict) -> None:
    m, runs = report["meta"], report["runs"]
    print(f"{m['queries']} queries, {m['docs']} docs / {m['chunks']} chunks, k={m['k']}, "
          f"embedding={m['models']['embedding']}, reranker={m['models']['reranker']}\n")

    print("Retrieval quality (permissions pre-filtered in every run)")
    rows = [[runs[key]["label"], *(f"{runs[key]['summary'][x]:.3f}" for x in QUALITY_METRICS),
             runs[key]["summary"]["p50_ms"]] for key in m["quality_runs"]]
    print(table(rows, ["config", *QUALITY_METRICS, "p50 ms"]) + "\n")

    tags = list(runs["vector/pre"]["by_tag"])
    print("nDCG@10 by query type")
    rows = [[runs[key]["label"], *(f"{runs[key]['by_tag'][t]['ndcg@10']:.3f}" for t in tags)] for key in m["quality_runs"]]
    print(table(rows, ["config", *(f"{t} (n={runs['vector/pre']['by_tag'][t]['n']})" for t in tags)]) + "\n")

    print("Permission enforcement")
    rows = []
    for key in m["acl_runs"]:
        s = runs[key]["summary"]
        rows.append([runs[key]["label"], f"{s['leak_rate']:.1%}", s["queries_with_leak"], s["sensitive_exposures"],
                     s["avg_returned"], s["short_result_queries"], f"{s['recall@10']:.3f}", f"{s['ndcg@10']:.3f}"])
    print(table(rows, ["config", "leaked results", "queries w/ leak", "sensitive docs shown",
                       "avg results", "queries < k", "recall@10", "ndcg@10"]) + "\n")

    base, best = runs["vector/pre"]["per_query"], runs["hybrid_rerank/pre"]["per_query"]
    changed = [(a, b) for a, b in zip(base, best) if a.get("first_relevant_rank") != b.get("first_relevant_rank")]
    print("Queries where the first relevant result moved (vector only -> hybrid + rerank)")
    rows = [[a["id"], a["user"], a["query"][:52], ",".join(a["tags"]), a["first_relevant_rank"] or "-",
             b["first_relevant_rank"] or "-"] for a, b in changed]
    print(table(rows, ["id", "user", "query", "tags", "vector", "hybrid+rerank"]))


def show_query(qid: str) -> None:
    pipeline = RetrievalPipeline()
    q = next(q for q in load_queries() if q["id"] == qid)
    print(f"{q['id']} [{q['user']}] {q['query']}   relevant={q['relevant']} sensitive={q.get('sensitive', [])}\n")
    for mode in ("vector", "bm25", "hybrid", "vector_rerank", "hybrid_rerank"):
        out = pipeline.search(q["query"], q["user"], mode=mode, acl="pre", k=5)
        marks = [f"{r['doc_id']}{'*' if r['doc_id'] in q['relevant'] else ''}" for r in out["results"]]
        print(f"{mode:14} {' > '.join(marks)}")


def compare_rerankers(names: list[str]) -> None:
    pipeline, queries, rows = RetrievalPipeline(), load_queries(), []
    for name in names:
        pipeline._reranker = Reranker(name)
        for mode in ("vector_rerank", "hybrid_rerank"):
            run = run_config(pipeline, queries, mode, "pre")
            rows.append({"reranker": name, "mode": mode, "summary": run["summary"],
                         "by_tag": {t: v["ndcg@10"] for t, v in run["by_tag"].items()}})
            s = run["summary"]
            print(f"{name:36} {mode:14} hit@1={s['hit@1']:.3f} mrr@10={s['mrr@10']:.3f} "
                  f"ndcg@10={s['ndcg@10']:.3f} p50={s['p50_ms']}ms", flush=True)
    (OUT.parent / "reranker_comparison.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", help="inspect one query id instead of running the suite")
    ap.add_argument("--rerankers", nargs="+", help="compare these fastembed cross-encoders")
    args = ap.parse_args()
    if args.rerankers:
        compare_rerankers(args.rerankers)
    elif args.query:
        show_query(args.query)
    else:
        report = run_all(progress=lambda i, n, name: print(f"[{i + 1}/{n}] {name}", flush=True))
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(json.dumps(report, indent=1))
        print(f"\nfinished in {report['meta']['duration_s']}s, wrote {OUT.relative_to(OUT.parent.parent)}\n")
        print_report(report)
