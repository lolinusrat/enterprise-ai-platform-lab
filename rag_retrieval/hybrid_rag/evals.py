"""Retrieval evaluation: graded relevance judgments per (user, query), standard IR metrics at the
document level, and permission metrics (leakage, sensitive-document exposure, post-filter starvation)."""
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from .acl import can_read
from .pipeline import MODE_LABELS, RetrievalPipeline

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ROOT / "data" / "queries.jsonl"
K = 10

# Retrieval quality: every mode with permissions enforced the same way (pre-filter).
QUALITY_RUNS = [(m, "pre") for m in ("vector", "bm25", "hybrid", "vector_rerank", "hybrid_rerank")]
# Permission enforcement: the baseline and the full pipeline under each ACL strategy.
ACL_RUNS = [(m, a) for m in ("vector", "hybrid_rerank") for a in ("none", "post", "pre")]


def load_queries(path: Path = QUERIES) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def validate(pipeline: RetrievalPipeline, queries: list[dict]) -> None:
    """Judgments may only mark documents the asking user can read as relevant."""
    for q in queries:
        user = pipeline.users[q["user"]]
        for doc_id in [*q["relevant"], *q.get("sensitive", [])]:
            if doc_id not in pipeline.docs:
                raise ValueError(f"{q['id']}: unknown doc {doc_id}")
        for doc_id in q["relevant"]:
            d = pipeline.docs[doc_id]
            if not can_read(user, d.classification, d.groups):
                raise ValueError(f"{q['id']}: {doc_id} is judged relevant but {user.id} can't read it")
        for doc_id in q.get("sensitive", []):
            d = pipeline.docs[doc_id]
            if can_read(user, d.classification, d.groups):
                raise ValueError(f"{q['id']}: sensitive doc {doc_id} is readable by {user.id}")


def dcg(gains: list[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def score_query(ranked_docs: list[str], relevant: dict[str, int], k: int = K) -> dict:
    top = ranked_docs[:k]
    rel_ranks = [i + 1 for i, d in enumerate(top) if relevant.get(d, 0) > 0]
    gains = [2 ** relevant.get(d, 0) - 1 for d in top]
    ideal = sorted((2 ** g - 1 for g in relevant.values()), reverse=True)[:k]
    n_rel = sum(1 for g in relevant.values() if g > 0)
    return {
        "hit@1": float(bool(top) and relevant.get(top[0], 0) > 0),
        "recall@5": sum(1 for d in top[:5] if relevant.get(d, 0) > 0) / n_rel,
        "recall@10": len(rel_ranks) / n_rel,
        "mrr@10": 1 / rel_ranks[0] if rel_ranks else 0.0,
        "ndcg@10": dcg(gains) / dcg(ideal) if ideal else 0.0,
        "first_relevant_rank": rel_ranks[0] if rel_ranks else None,
    }


QUALITY_METRICS = ("hit@1", "recall@5", "recall@10", "mrr@10", "ndcg@10")


def run_config(pipeline: RetrievalPipeline, queries: list[dict], mode: str, acl: str, k: int = K) -> dict:
    per_query, latencies = [], []
    for q in queries:
        out = pipeline.search(q["query"], q["user"], mode=mode, acl=acl, k=k)
        latencies.append(out["timings"]["total_ms"])
        docs = [r["doc_id"] for r in out["results"]]
        row = {
            "id": q["id"],
            "user": q["user"],
            "query": q["query"],
            "tags": q["tags"],
            "docs": docs,
            "returned": len(docs),
            "leaked": out["acl_stats"]["leaked"],
            "post_filter_dropped": out["acl_stats"]["post_filter_dropped"],
            "sensitive_exposed": [d for d in q.get("sensitive", []) if d in docs],
            "latency_ms": out["timings"]["total_ms"],
        }
        if q["relevant"]:
            row.update(score_query(docs, q["relevant"], k))
        per_query.append(row)
    return {
        "mode": mode,
        "acl": acl,
        "label": f"{MODE_LABELS[mode]} · {acl}-filter" if acl != "none" else f"{MODE_LABELS[mode]} · no filter",
        "summary": summarize(per_query, latencies, k),
        "by_tag": by_tag(per_query),
        "per_query": per_query,
    }


def summarize(rows: list[dict], latencies: list[float], k: int = K) -> dict:
    judged = [r for r in rows if "ndcg@10" in r]
    out = {m: round(statistics.mean(r[m] for r in judged), 4) for m in QUALITY_METRICS}
    total_returned = sum(r["returned"] for r in rows)
    out.update({
        "queries": len(rows),
        "judged_queries": len(judged),
        "leak_rate": round(sum(r["leaked"] for r in rows) / max(total_returned, 1), 4),
        "queries_with_leak": sum(1 for r in rows if r["leaked"]),
        "sensitive_exposures": sum(len(r["sensitive_exposed"]) for r in rows),
        "avg_returned": round(total_returned / len(rows), 2),
        "short_result_queries": sum(1 for r in rows if r["returned"] < k),
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(sorted(latencies)[max(0, math.ceil(0.95 * len(latencies)) - 1)], 1),
    })
    return out


def by_tag(rows: list[dict]) -> dict:
    tags = sorted({t for r in rows for t in r["tags"]})
    out = {}
    for t in tags:
        judged = [r for r in rows if t in r["tags"] and "ndcg@10" in r]
        if judged:
            out[t] = {m: round(statistics.mean(r[m] for r in judged), 4) for m in QUALITY_METRICS}
            out[t]["n"] = len(judged)
    return out


def run_all(pipeline: RetrievalPipeline | None = None, progress=None) -> dict:
    pipeline = pipeline or RetrievalPipeline()
    queries = load_queries()
    validate(pipeline, queries)
    runs = []
    configs = list(dict.fromkeys(QUALITY_RUNS + ACL_RUNS))
    started = time.perf_counter()
    pipeline.search("warm up", "ana", mode="hybrid_rerank")  # load the reranker before timing
    for i, (mode, acl) in enumerate(configs):
        if progress:
            progress(i, len(configs), f"{mode}/{acl}")
        runs.append(run_config(pipeline, queries, mode, acl))
    return {
        "meta": {
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_s": round(time.perf_counter() - started, 1),
            "k": K,
            "queries": len(queries),
            "docs": len(pipeline.docs),
            "chunks": len(pipeline.chunks),
            "models": pipeline.models,
            "settings": vars(pipeline.settings),
            "quality_runs": [f"{m}/{a}" for m, a in QUALITY_RUNS],
            "acl_runs": [f"{m}/{a}" for m, a in ACL_RUNS],
        },
        "runs": {f"{r['mode']}/{r['acl']}": r for r in runs},
    }
