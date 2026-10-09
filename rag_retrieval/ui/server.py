"""Demo UI: side-by-side search for any persona, the pipeline, eval results and the corpus ACL matrix.

    uv run uvicorn ui.server:app --port 8766      then open http://localhost:8766
"""
import json
import threading
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from hybrid_rag.acl import LEVELS, can_read, deny_reason
from hybrid_rag.answer import ANSWER_MODEL, answer
from hybrid_rag.evals import load_queries, run_all
from hybrid_rag.pipeline import ACL_MODES, MODE_LABELS, MODES, RetrievalPipeline

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"

app = FastAPI(title="Hybrid RAG retrieval demo")
pipeline = RetrievalPipeline()
pipeline.search("warm up", "ana")  # load the reranker before the first request
lock = threading.Lock()  # ONNX sessions are shared; keep searches serialized
jobs: dict[str, dict] = {}


class SearchReq(BaseModel):
    query: str
    user: str
    mode: str = "hybrid_rerank"
    acl: str = "pre"
    k: int = 5


class AnswerReq(SearchReq):
    pass


def check(req: SearchReq):
    if req.user not in pipeline.users or req.mode not in MODES or req.acl not in ACL_MODES or not 1 <= req.k <= 20:
        raise HTTPException(400, "unknown user, mode or acl, or k out of range")
    if not req.query.strip():
        raise HTTPException(400, "empty query")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/config")
def config():
    return {
        "users": [vars(u) | {"groups": list(u.groups)} for u in pipeline.users.values()],
        "modes": [{"id": m, "label": MODE_LABELS[m]} for m in MODES],
        "acl_modes": list(ACL_MODES),
        "models": pipeline.models | {"answer": ANSWER_MODEL},
        "settings": vars(pipeline.settings),
        "queries": load_queries(),
        "stats": {"docs": len(pipeline.docs), "chunks": len(pipeline.chunks)},
    }


@app.post("/api/search")
def search(req: SearchReq):
    check(req)
    with lock:
        return pipeline.search(req.query, req.user, req.mode, req.acl, req.k)


@app.post("/api/answer")
def generate(req: AnswerReq):
    check(req)
    with lock:
        out = pipeline.search(req.query, req.user, req.mode, req.acl, req.k)
    try:
        return answer(req.query, out["results"]) | {"acl": req.acl, "leaked_sources": out["acl_stats"]["leaked"]}
    except httpx.HTTPError as e:
        raise HTTPException(503, f"Ollama is not reachable or the model {ANSWER_MODEL} is not pulled: {e}")


@app.get("/api/corpus")
def corpus():
    users = list(pipeline.users.values())
    return {
        "levels": LEVELS,
        "docs": [
            {
                "id": d.id, "title": d.title, "department": d.department, "classification": d.classification,
                "groups": list(d.groups), "updated": d.updated,
                "chunks": sum(1 for c in pipeline.chunks if c.doc_id == d.id),
                "access": {u.id: can_read(u, d.classification, d.groups) for u in users},
                "reasons": {u.id: deny_reason(u, d.classification, d.groups) for u in users},
            }
            for d in pipeline.docs.values()
        ],
    }


@app.get("/api/report")
def report():
    path = RESULTS / "eval_report.json"
    if not path.exists():
        raise HTTPException(404, "no eval report yet; run run_evals.py or start a run from the UI")
    out = json.loads(path.read_text())
    rr = RESULTS / "reranker_comparison.json"
    out["rerankers"] = json.loads(rr.read_text()) if rr.exists() else []
    return out


def _run_job(job_id: str):
    job = jobs[job_id]
    try:
        def progress(i, n, name):
            job.update(progress=i / n, step=name)
        with lock:
            rep = run_all(pipeline, progress=progress)
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / "eval_report.json").write_text(json.dumps(rep, indent=1))
        job.update(status="done", progress=1.0, step="done")
    except Exception as e:  # surfaced to the UI
        job.update(status="error", error=str(e))


@app.post("/api/evals")
def start_eval():
    if any(j["status"] == "running" for j in jobs.values()):
        raise HTTPException(409, "an eval run is already in progress")
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "status": "running", "progress": 0.0, "step": "starting"}
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return jobs[job_id]


@app.get("/api/evals/{job_id}")
def eval_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "unknown job")
    return jobs[job_id]
