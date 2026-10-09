"""HTTP API and UI.  uv run python run.py  ->  http://localhost:8090"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from secagent import agent, llm
from secagent.gateway import Session, Trace, bootstrap
from secagent.opa import OPA
from secagent.reference import AGENT_ID, EMPLOYEES, INSTRUCTION_MODES, PURPOSES, SCENARIOS

ROOT = Path(__file__).resolve().parent.parent
opa = OPA()
if not opa.healthy():
    raise SystemExit(f"OPA is not reachable at {opa.url}. Start it first: docker compose up -d")
gateway, authz, audit, key = bootstrap(opa)
app = FastAPI(title="Authorization-gated customer agent")


def scenario_prompt(scenario: dict) -> str:
    """Fill in scenario placeholders. The stolen-grant case needs a real grant issued to Alice elsewhere."""
    prompt = scenario["prompt"]
    if "{alice_grant}" in prompt:
        issued = authz.request(Session("u-alice", AGENT_ID), "C-1001", "account_servicing", "", Trace())
        prompt = prompt.replace("{alice_grant}", issued["grant"])
    return prompt


class RunRequest(BaseModel):
    scenario: str | None = None
    user: str | None = None
    mode: str | None = None
    prompt: str | None = None
    backend: str = "scripted"
    model: str | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "ui" / "index.html")


@app.get("/api/meta")
def meta() -> dict:
    return {"employees": EMPLOYEES, "modes": INSTRUCTION_MODES, "purposes": PURPOSES, "agent": AGENT_ID,
            "backends": llm.available_backends(), "opa": opa.url, "opa_healthy": opa.healthy(),
            "scenarios": [{k: v for k, v in s.items() if k != "expect"} for s in SCENARIOS]}


@app.post("/api/run")
def run(req: RunRequest) -> dict:
    sc = next((s for s in SCENARIOS if s["id"] == req.scenario), None)
    user = req.user or (sc and sc["user"])
    mode = req.mode or (sc and sc["mode"]) or "compliant"
    prompt = req.prompt or (sc and scenario_prompt(sc))
    if user not in EMPLOYEES or mode not in INSTRUCTION_MODES or not prompt:
        raise HTTPException(400, "need a known user, a mode and a prompt (or a scenario id)")
    if req.backend not in llm.available_backends():
        raise HTTPException(400, f"backend {req.backend!r} is not available")
    try:
        result = agent.run(gateway, Session(user, AGENT_ID), prompt, mode, req.backend, req.model)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    if sc:
        result["scenario"] = sc["id"]
        result["unexpected_release"] = sorted(set(result["released"]) - set(sc["may_release"]))
    return result


@app.get("/api/policies")
def policies() -> dict:
    files = ["grant.rego", "access.rego"]
    return {f: (ROOT / "policies" / f).read_text() for f in files}


@app.get("/api/audit")
def audit_log() -> list[dict]:
    return list(reversed(audit.events[-300:]))


@app.get("/api/health")
def health() -> dict:
    return {"opa": opa.healthy()}
