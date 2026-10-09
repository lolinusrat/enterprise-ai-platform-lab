"""Offline tests. The first run downloads the two ONNX models (~110 MB) into the fastembed cache."""
import pytest

from hybrid_rag.acl import Principal, can_read
from hybrid_rag.corpus import chunk_doc, load_corpus
from hybrid_rag.evals import load_queries, score_query, validate
from hybrid_rag.fusion import rrf
from hybrid_rag.lexical import BM25, tokenize
from hybrid_rag.pipeline import MODES, RetrievalPipeline


@pytest.fixture(scope="module")
def pipe():
    return RetrievalPipeline()


def P(groups, clearance):
    return Principal("t", "T", "test", tuple(groups), clearance)


# --- permission model -------------------------------------------------------

def test_access_needs_group_and_clearance():
    assert can_read(P(["hr"], "restricted"), "restricted", ("hr", "leadership"))
    assert not can_read(P(["hr"], "confidential"), "restricted", ("hr",))       # clearance too low
    assert not can_read(P(["engineering"], "restricted"), "restricted", ("hr",))  # no matching group
    assert can_read(P(["contractors"], "internal"), "public", ("everyone",))     # everyone is implicit


@pytest.mark.parametrize("mode", list(MODES))
def test_prefilter_never_returns_forbidden(pipe, mode):
    for q in load_queries():
        out = pipe.search(q["query"], q["user"], mode=mode, acl="pre", k=10)
        user = pipe.users[q["user"]]
        for r in out["results"]:
            assert can_read(user, r["classification"], r["groups"]), (q["id"], r["doc_id"])


def test_no_filter_leaks_the_salary_bands(pipe):
    """The baseline the filter protects against: an engineer asking about pay sees restricted bands."""
    out = pipe.search("salary range for an L4 engineer", "ana", mode="vector", acl="none", k=5)
    assert out["results"][0]["doc_id"] == "hr-salary-bands-2026"
    assert out["acl_stats"]["leaked"] >= 1
    pre = pipe.search("salary range for an L4 engineer", "ana", mode="vector", acl="pre", k=5)
    assert "hr-salary-bands-2026" not in [r["doc_id"] for r in pre["results"]]
    assert pre["acl_stats"]["would_surface"] >= 1


def test_post_filter_returns_short_lists(pipe):
    post = pipe.search("salary range for an L4 engineer", "ana", acl="post", k=5)
    pre = pipe.search("salary range for an L4 engineer", "ana", acl="pre", k=5)
    assert len(post["results"]) < 5 == len(pre["results"])
    assert post["acl_stats"]["post_filter_dropped"] == 5 - len(post["results"])


def test_holders_of_access_still_find_restricted_docs(pipe):
    out = pipe.search("salary range for an L4 engineer", "priya", acl="pre", k=3)
    assert out["results"][0]["doc_id"] == "hr-salary-bands-2026"


# --- retrieval components ---------------------------------------------------

def test_tokenizer_keeps_identifiers_whole_and_split():
    toks = tokenize("Error PAY-4031 and max_client_conn")
    assert {"pay-4031", "pay", "4031", "max_client_conn", "client"} <= set(toks)


def test_bm25_ranks_exact_identifier_first():
    bm = BM25(["incident INC-2287 search", "incident INC-2291 checkout", "incident INC-2302 checkout"])
    assert bm.search("INC-2291", 3)[0][0] == 1
    masked = bm.search("INC-2291", 3, mask=[True, False, True])  # filtered before ranking
    assert masked and 1 not in [i for i, _ in masked]


def test_rrf_rewards_agreement():
    fused = rrf({"a": [1, 2, 3], "b": [2, 3, 1]})
    assert fused[0][0] == 2


def test_hybrid_fixes_identifier_query_vector_misses(pipe):
    vec = [r["doc_id"] for r in pipe.search("INC-2291 root cause", "ana", mode="vector", k=3)["results"]]
    hyb = pipe.search("INC-2291 root cause", "ana", mode="hybrid_rerank", k=3)["results"]
    assert "eng-postmortem-inc-2291" not in vec
    assert hyb[0]["doc_id"] == "eng-postmortem-inc-2291"
    assert hyb[0]["provenance"]["bm25_rank"] == 1


def test_results_are_one_chunk_per_doc(pipe):
    docs = [r["doc_id"] for r in pipe.search("checkout latency postmortem", "ana", k=10)["results"]]
    assert len(docs) == len(set(docs))


# --- corpus and evaluation --------------------------------------------------

def test_chunker_merges_short_paragraphs():
    _, chunks = load_corpus()
    assert all(len(c.text) >= 60 for c in chunks)
    assert len({c.id for c in chunks}) == len(chunks)


def test_judgments_are_consistent_with_acl(pipe):
    validate(pipe, load_queries())  # raises if a relevant doc is unreadable or a sensitive one readable


def test_metrics():
    m = score_query(["x", "a", "b"], {"a": 2, "c": 1})
    assert m["hit@1"] == 0 and m["mrr@10"] == 0.5 and m["recall@10"] == 0.5
    assert 0 < m["ndcg@10"] < 1
    assert score_query(["a"], {"a": 2})["ndcg@10"] == 1.0
