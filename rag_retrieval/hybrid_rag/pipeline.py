"""The retrieval pipeline: permission filter -> BM25 + dense -> RRF -> cross-encoder rerank -> top-k docs.

`mode` picks the retrievers and whether to rerank; `acl` picks where permissions are enforced:
  pre   restrict both indexes to the reader's permitted chunks before ranking (the design used here)
  post  rank over everything, then drop what the reader can't see (common, but returns fewer results)
  none  no enforcement; only for measuring what would leak
"""
import time
from dataclasses import dataclass

import numpy as np

from .acl import Principal, can_read, deny_reason, load_users
from .corpus import load_corpus
from .dense import EMBED_MODEL, DenseIndex
from .fusion import RRF_K, rrf
from .lexical import BM25
from .rerank import RERANK_MODEL, Reranker

MODES = {
    # name: (vector, bm25, rerank)
    "vector": (True, False, False),
    "bm25": (False, True, False),
    "hybrid": (True, True, False),
    "vector_rerank": (True, False, True),
    "hybrid_rerank": (True, True, True),
}
MODE_LABELS = {
    "vector": "Vector only",
    "bm25": "BM25 only",
    "hybrid": "Hybrid (RRF)",
    "vector_rerank": "Vector + rerank",
    "hybrid_rerank": "Hybrid + rerank",
}
ACL_MODES = ("pre", "post", "none")


@dataclass
class Settings:
    depth: int = 50          # candidates taken from each first-stage retriever
    rerank_depth: int = 30   # fused candidates scored by the cross-encoder
    rrf_k: int = RRF_K


class RetrievalPipeline:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.docs, self.chunks = load_corpus()
        self.users = load_users()
        self.texts = [c.indexed_text(self.docs[c.doc_id].title) for c in self.chunks]
        self.bm25 = BM25(self.texts)
        self.dense = DenseIndex(self.texts)
        self._reranker: Reranker | None = None
        self.models = {"embedding": EMBED_MODEL, "reranker": RERANK_MODEL}

    @property
    def reranker(self) -> Reranker:
        if self._reranker is None:
            self._reranker = Reranker()
        return self._reranker

    def permitted_mask(self, principal: Principal) -> np.ndarray:
        return np.array([can_read(principal, c.classification, c.groups) for c in self.chunks])

    def search(self, query: str, principal: Principal | str, mode: str = "hybrid_rerank",
               acl: str = "pre", k: int = 10) -> dict:
        if mode not in MODES or acl not in ACL_MODES:
            raise ValueError(f"unknown mode {mode!r} or acl {acl!r}")
        if isinstance(principal, str):
            principal = self.users[principal]
        use_vec, use_bm25, use_rerank = MODES[mode]
        st = self.settings
        timings: dict[str, float] = {}
        t0 = time.perf_counter()

        allowed = self.permitted_mask(principal)
        mask = allowed if acl == "pre" else None
        timings["acl_filter_ms"] = (time.perf_counter() - t0) * 1000

        vec_hits, lex_hits, qvec = [], [], None
        if use_vec:
            t = time.perf_counter()
            qvec = self.dense.embed_query(query)
            vec_hits = self.dense.search(query, st.depth, mask, qvec=qvec)
            timings["vector_ms"] = (time.perf_counter() - t) * 1000
        if use_bm25:
            t = time.perf_counter()
            lex_hits = self.bm25.search(query, st.depth, mask)
            timings["bm25_ms"] = (time.perf_counter() - t) * 1000

        vec_rank = {i: r for r, (i, _) in enumerate(vec_hits, 1)}
        lex_rank = {i: r for r, (i, _) in enumerate(lex_hits, 1)}
        vec_score = dict(vec_hits)
        lex_score = dict(lex_hits)

        t = time.perf_counter()
        if use_vec and use_bm25:
            ranked = rrf({"vector": [i for i, _ in vec_hits], "bm25": [i for i, _ in lex_hits]}, k=st.rrf_k)
        else:
            ranked = vec_hits or lex_hits
        timings["fusion_ms"] = (time.perf_counter() - t) * 1000
        fused_rank = {i: r for r, (i, _) in enumerate(ranked, 1)}
        fused_score = dict(ranked)

        rerank_score: dict[int, float] = {}
        if use_rerank and ranked:
            t = time.perf_counter()
            pool = [i for i, _ in ranked[: st.rerank_depth]]
            scores = self.reranker.score(query, [self.texts[i] for i in pool])
            rerank_score = dict(zip(pool, scores))
            ranked = sorted(rerank_score.items(), key=lambda kv: -kv[1])
            timings["rerank_ms"] = (time.perf_counter() - t) * 1000

        # Collapse chunks to documents: each document is represented by its best chunk.
        results, seen = [], set()
        for i, score in ranked:
            c = self.chunks[i]
            if c.doc_id in seen:
                continue
            seen.add(c.doc_id)
            results.append(self._result(i, score, principal, allowed[i], vec_rank, lex_rank, fused_rank,
                                        vec_score, lex_score, fused_score, rerank_score))
            if len(results) == k:
                break

        dropped = 0
        if acl == "post":
            dropped = sum(not r["allowed"] for r in results)
            results = [r for r in results if r["allowed"]]
        for rank, r in enumerate(results, 1):
            r["rank"] = rank
        timings["total_ms"] = (time.perf_counter() - t0) * 1000

        return {
            "query": query,
            "user": principal.id,
            "mode": mode,
            "acl": acl,
            "k": k,
            "results": results,
            "acl_stats": {
                "permitted_chunks": int(allowed.sum()),
                "total_chunks": len(self.chunks),
                "post_filter_dropped": dropped,
                "leaked": sum(not r["allowed"] for r in results),
                "would_surface": self._would_surface(query, qvec, allowed, use_vec, use_bm25, k) if acl == "pre" else None,
            },
            "candidates": {"vector": len(vec_hits), "bm25": len(lex_hits), "fused": len(fused_rank),
                           "reranked": len(rerank_score)},
            "timings": {name: round(ms, 2) for name, ms in timings.items()},
        }

    def _would_surface(self, query, qvec, allowed, use_vec, use_bm25, k) -> int:
        """How many forbidden documents an unfiltered first stage would have put in the top k.
        Only the count is returned, never titles: naming them would itself leak."""
        hits = []
        if use_vec:
            hits += self.dense.search(query, k, None, qvec=qvec)
        if use_bm25:
            hits += self.bm25.search(query, k)
        return len({self.chunks[i].doc_id for i, _ in hits if not allowed[i]})

    def _result(self, i, score, principal, allowed, vec_rank, lex_rank, fused_rank,
                vec_score, lex_score, fused_score, rerank_score) -> dict:
        c = self.chunks[i]
        d = self.docs[c.doc_id]
        return {
            "chunk_id": c.id,
            "doc_id": c.doc_id,
            "title": d.title,
            "department": d.department,
            "classification": d.classification,
            "groups": list(d.groups),
            "updated": d.updated,
            "text": c.text,
            "score": round(float(score), 4),
            "allowed": bool(allowed),
            "deny_reason": None if allowed else deny_reason(principal, c.classification, c.groups),
            "provenance": {
                "vector_rank": vec_rank.get(i), "vector_score": _r(vec_score.get(i)),
                "bm25_rank": lex_rank.get(i), "bm25_score": _r(lex_score.get(i)),
                "fused_rank": fused_rank.get(i), "fused_score": _r(fused_score.get(i), 5),
                "rerank_score": _r(rerank_score.get(i)),
            },
        }


def _r(x, n=4):
    return None if x is None else round(float(x), n)
