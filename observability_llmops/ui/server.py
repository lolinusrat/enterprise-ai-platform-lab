"""Demo UI: live traced runs, the architecture and flow, and eval runs gated against the baseline.

    uv run uvicorn ui.server:app --port 8765      then open http://localhost:8765
"""
import json
import os
import threading
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from obs_agent.evals import compare, load_dataset, run_suite, strip_spans
from obs_agent.graph import AGENT_PROMPTS, CLASSIFY_PROMPT, build_graph
from obs_agent.models import DEFAULT_MODELS, MODEL_CHOICES
from obs_agent.runner import run_traced
from obs_agent.telemetry import CAPTURE_CONTENT, setup_tracing
from obs_agent.usage import PRICES

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
BACKENDS = ["groq", "ollama", "scripted"] if os.environ.get("GROQ_API_KEY") else ["ollama", "scripted"]

app = FastAPI(title="LangGraph observability demo")
setup_tracing()
jobs: dict[str, dict] = {}


@lru_cache(maxsize=8)
def graph_for(backend: str, prompt: str, model: str | None = None):
    return build_graph(backend, model, prompt)


def check(backend, prompt, model):
    if backend not in BACKENDS or prompt not in AGENT_PROMPTS or (model and model not in MODEL_CHOICES[backend]):
        raise HTTPException(400, "unknown backend, prompt or model")


def baseline_path(backend: str) -> Path:
    return EVALS / ("baseline.json" if backend == "groq" else f"baseline.{backend}.json")


def result_name(report: dict) -> str:
    m = report["meta"]
    return f"{m['started_at'].replace(':', '')}_{m['backend']}_{m['model'].split('/')[-1]}_{m['prompt_version']}.json"


def thresholds() -> dict:
    return json.loads((EVALS / "thresholds.json").read_text())


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/config")
def config():
    g = graph_for("scripted", "v1").get_graph()
    return {
        "backends": BACKENDS, "default_models": DEFAULT_MODELS, "models": MODEL_CHOICES, "prices_per_1m": PRICES,
        "prompts": AGENT_PROMPTS, "classify_prompt": CLASSIFY_PROMPT, "thresholds": thresholds(),
        "capture_content": CAPTURE_CONTENT, "otlp_endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
        "jaeger_ui": os.environ.get("JAEGER_UI", "http://localhost:16686") if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") else None,
        "graph": {"nodes": list(g.nodes), "edges": [[e.source, e.target, e.conditional] for e in g.edges]},
        "dataset": load_dataset(EVALS / "dataset.jsonl"),
    }


class RunRequest(BaseModel):
    question: str
    backend: str = "groq"
    prompt: str = "v1"
    model: str | None = None


@app.post("/api/run")
def run(req: RunRequest):
    check(req.backend, req.prompt, req.model)
    return run_traced(graph_for(req.backend, req.prompt, req.model), req.question,
                      {"support.prompt_version": req.prompt, "app.source": "ui"})


class EvalRequest(BaseModel):
    backend: str = "groq"
    prompt: str = "v1"
    model: str | None = None
    repeats: int = 1


@app.post("/api/evals")
def start_eval(req: EvalRequest):
    check(req.backend, req.prompt, req.model)
    if not 1 <= req.repeats <= 5:
        raise HTTPException(400, "repeats must be 1-5")
    job_id = uuid.uuid4().hex[:8]
    dataset = load_dataset(EVALS / "dataset.jsonl")
    job = jobs[job_id] = {"id": job_id, "status": "running", "total": len(dataset), "done": [],
                          "report": None, "verdict": None, "error": None, **req.model_dump()}

    def work():
        try:
            report = run_suite(dataset, req.backend, req.model, req.prompt, req.repeats, 4,
                               on_case=lambda row: job["done"].append(row))
            out = EVALS / "results" / result_name(report)
            out.parent.mkdir(exist_ok=True)
            out.write_text(json.dumps(report, indent=2, default=str))
            bp = baseline_path(req.backend)
            job["verdict"] = compare(json.loads(bp.read_text()), report, thresholds()) if bp.exists() else None
            job["report"] = report
            job["status"] = "done"
        except Exception as e:
            job["error"] = f"{type(e).__name__}: {e}"
            job["status"] = "error"

    threading.Thread(target=work, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/evals/{job_id}")
def eval_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    return jobs[job_id]


@app.get("/api/baseline/{backend}")
def get_baseline(backend: str):
    p = baseline_path(backend)
    if not p.exists():
        raise HTTPException(404, "no baseline")
    return json.loads(p.read_text())


@app.post("/api/baseline/from-job/{job_id}")
def promote(job_id: str):
    job = jobs.get(job_id)
    if not job or not job["report"]:
        raise HTTPException(404)
    baseline_path(job["backend"]).write_text(json.dumps(strip_spans(job["report"]), indent=2, default=str))
    return {"ok": True}


@app.get("/api/results")
def list_results():
    files = sorted((EVALS / "results").glob("*.json"), reverse=True)
    return [f.name for f in files]


@app.get("/api/results/{name}")
def get_result(name: str):
    p = (EVALS / "results" / name).resolve()
    if p.parent != (EVALS / "results").resolve() or not p.exists():
        raise HTTPException(404)
    report = json.loads(p.read_text())
    bp = baseline_path(report["meta"]["backend"])
    verdict = compare(json.loads(bp.read_text()), report, thresholds()) if bp.exists() else None
    return {"report": report, "verdict": verdict}
