"""Shared gateways (port 8081): the three policy enforcement points.

  POST /agents/{bu}/{name}/invoke   Agent Gateway  (front door; user token in, trace out)
  POST /v1/model                    Model Gateway  (only egress to models)
  POST /v1/tools/{tool}             Tool Gateway   (only path to enterprise systems)

Each verifies tokens locally against cached JWKS keys, asks the local PDP (OPA) for a
decision, enforces it, and writes one audit event per hop. In production these would be
three separately scaled deployments (e.g. Envoy + ext_authz); here they share a process.
"""
import time
import uuid

import httpx
import jwt
from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import pii
from .common import (BROKER_ISS, CP_URL, DEFAULT_STANDIN, GATEWAY_TOKEN, IDP_ISS, MODEL_BACKEND, OLLAMA_URL, RUNTIME_URL,
                     SERVICE_TOKEN, STANDINS, opa, step, summarize)
from .identity import Verifier
from .reference import CLASSES, TOOLS

app = FastAPI(title="Agent / Model / Tool Gateways")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"],
                   allow_methods=["*"], allow_headers=["*"])
verifier = Verifier(f"{CP_URL}/.well-known/jwks.json")
http = httpx.AsyncClient(timeout=180)
SVC = {"X-Service-Token": SERVICE_TOKEN}


async def audit(**e) -> dict:
    r = await http.post(f"{CP_URL}/api/audit", json=e, headers=SVC)
    r.raise_for_status()
    return r.json()


def bearer(h: str | None) -> str:
    return (h or "").removeprefix("Bearer ").strip()


# ---------------- Agent Gateway ----------------
class InvokeReq(BaseModel):
    kind: str = "model"          # model | tool
    model: str | None = None
    cls: int = 1                 # classification label the caller declares for the prompt
    prompt: str = ""
    tool: str | None = None
    args: dict = {}


@app.post("/agents/{bu}/{name}/invoke")
async def invoke(bu: str, name: str, req: InvokeReq, authorization: str | None = Header(None)):
    agent, corr, t0 = f"{bu}/{name}", uuid.uuid4().hex, time.perf_counter()
    action = f"model.call {req.model} [{CLASSES[req.cls]}]" if req.kind == "model" else f"tool.call {req.tool}"
    steps, events = [], []

    try:
        user = verifier.verify(bearer(authorization), audience="agent-gateway", issuer=IDP_ISS)
        steps.append(step("Agent Gateway", "Authenticate user", "pass",
                          f"OIDC token for {user['sub']} verified (issuer idp.corp.example, {'MFA satisfied' if 'mfa' in user.get('amr', []) else 'no MFA'}).", "GR-01"))
    except jwt.PyJWTError as e:
        steps.append(step("Agent Gateway", "Authenticate user", "deny", f"User token rejected: {e}.", "GR-01"))
        user = {"sub": "anonymous", "bu": "-"}

    if steps[-1]["status"] != "deny":
        inv = await opa(http, "controlplane/runtime/invoke", {"agent": agent, "user": {k: user[k] for k in ("sub", "bu", "role", "groups")}})
        steps += inv["steps"]

    inv_decision, key = summarize(steps)
    events.append(await audit(kind="runtime", bu=bu, agent=agent, actor=user["sub"], action=f"invoke · {action}",
                              decision=inv_decision, rule=key["rule"], detail=key["detail"], corr=corr))

    output, served = None, None
    if inv_decision != "deny":
        r = await http.post(f"{RUNTIME_URL}/run/{bu}/{name}", json=req.model_dump(),
                            headers={"Authorization": f"Bearer {bearer(authorization)}", "X-Correlation-Id": corr, "X-Gateway-Token": GATEWAY_TOKEN})
        body = r.json()
        steps += body.get("steps", [])
        events += body.get("events", [])
        output, served = body.get("output"), body.get("served_model")
        action = body.get("action", action)

    decision, key = summarize(steps)
    seqs = ", ".join(f"#{e['seq']}" for e in events)
    steps.append(step("Audit Ledger", "Record decision", "pass",
                      f"{len(events)} event(s) appended under correlation id {corr[:8]}: {seqs}. Head {events[-1]['hash'][:12]}…", "GR-08"))
    return {"correlation_id": corr, "agent": agent, "user": user["sub"], "action": action, "decision": decision, "key": key,
            "steps": steps, "events": events, "output": output, "served_model": served, "ms": round((time.perf_counter() - t0) * 1000)}


# ---------------- shared caller verification for model and tool gateways ----------------
def caller(authorization: str | None, x_svid: str | None, gw: str):
    """Agent proves who it is (SVID) and whom it acts for (OBO token); act.sub must match the SVID."""
    svid = verifier.verify(x_svid or "", audience=gw, issuer=BROKER_ISS)
    obo = verifier.verify(bearer(authorization), audience=gw, issuer=BROKER_ISS)
    if obo["act"]["sub"] != svid["sub"]:
        raise jwt.InvalidTokenError("OBO token was issued to a different agent")
    return svid, obo


def denied(comp, e):
    s = step(comp, "Verify caller", "deny", f"Caller tokens rejected: {e}.", "GR-01")
    return JSONResponse({"decision": "deny", "steps": [s], "events": []}, status_code=401)


# ---------------- Model Gateway ----------------
class ModelReq(BaseModel):
    model: str
    cls: int
    messages: list[dict]


async def complete(model: str, messages: list[dict]) -> tuple[str, int, str]:
    standin = STANDINS.get(model, DEFAULT_STANDIN)
    if MODEL_BACKEND == "ollama":
        try:
            r = await http.post(f"{OLLAMA_URL}/api/chat", json={"model": standin, "messages": messages, "stream": False,
                                                                   "options": {"num_predict": 160, "temperature": 0.2}})
            r.raise_for_status()
            j = r.json()
            return j["message"]["content"].strip(), j.get("prompt_eval_count", 0) + j.get("eval_count", 0), f"ollama/{standin}"
        except httpx.HTTPError:
            pass
    text = messages[-1]["content"]
    return f"[mock model] Received {len(text.split())} words. First line: {text[:120]}", len(text) // 4, "mock"


@app.post("/v1/model")
async def model(req: ModelReq, authorization: str | None = Header(None), x_svid: str | None = Header(None),
                x_correlation_id: str = Header("")):
    try:
        svid, obo = caller(authorization, x_svid, "model-gateway")
    except jwt.PyJWTError as e:
        return denied("Model Gateway", e)
    agent, bu = svid["agent"], svid["agent"].split("/")[0]
    steps = [step("Model Gateway", "Verify caller", "pass", f"SVID {svid['sub']} and on-behalf-of token for {obo['sub']} (act matches).", "GR-01")]

    user_msgs = [m for m in req.messages if m["role"] == "user"]
    spans = [s for m in user_msgs for s in pii.find(m["content"])]
    cls = max(req.cls, 2) if spans else req.cls     # detected personal data is at least Confidential
    d = await opa(http, "controlplane/runtime/model", {"agent": agent, "model": req.model, "cls": cls, "declared_cls": req.cls,
                                                       "pii_entities": [k for k, _, _ in spans]})
    steps += d["steps"]
    route = d["route"] or req.model
    action = f"model.call {req.model}{' → ' + route if route != req.model else ''} [{CLASSES[cls]}{', PII ' + str(len(spans)) if spans else ''}]"

    output, served, tokens = None, None, 0
    if d["decision"] != "deny":
        msgs = [{**m, "content": pii.redact(m["content"], pii.find(m["content"]))} if d["redact"] and m["role"] == "user" else m
                for m in req.messages]
        output, tokens, backend = await complete(route, msgs)
        served = f"{route} (stand-in: {backend})"
        if d["redact"]:
            output = f"Prompt as sent: {msgs[-1]['content']}\n\n{output}"
        await http.post(f"{CP_URL}/api/usage", json={"bu": bu, "agent": agent, "tokens": tokens}, headers=SVC)

    decision, key = summarize(steps)
    ev = await audit(kind="runtime", bu=bu, agent=agent, actor=obo["sub"], action=action, decision=decision,
                     rule=key["rule"], detail=key["detail"] + (f" Tokens metered: {tokens}." if tokens else ""), corr=x_correlation_id)
    return JSONResponse({"decision": decision, "steps": steps, "events": [ev], "output": output, "served_model": served,
                         "action": action, "tokens": tokens}, status_code=403 if decision == "deny" else 200)


# ---------------- Tool Gateway ----------------
MOCK_SYSTEMS = {
    "crm.read": lambda a: {"client": a.get("client", "C-20931"), "segment": "Premier", "last_contact": "2026-09-18"},
    "crm.write": lambda a: {"note_id": "N-88412", "saved": True},
    "core-banking.read": lambda a: {"account": a.get("account", "062-000 12345678"), "balance": 4210.55, "disputes_12m": 2},
    "payments.refund": lambda a: {"refund_id": "R-55120", "amount": a.get("amount")},
    "portfolio.read": lambda a: {"holdings": 14, "risk_profile": "Balanced"},
    "research.search": lambda a: {"hits": 3},
    "hris.read": lambda a: {"annual_leave_days": 12.5},
    "policy-kb.search": lambda a: {"hits": 5, "top": "Leave policy v7"},
    "cms.publish": lambda a: {"page": "/campaigns/q4-home-loans", "published": True},
    "analytics.read": lambda a: {"sessions_7d": 18430},
    "web.search": lambda a: {"hits": 10},
}


class ToolReq(BaseModel):
    args: dict = {}


@app.post("/v1/tools/{tool}")
async def tool(tool: str, req: ToolReq, authorization: str | None = Header(None), x_svid: str | None = Header(None),
               x_correlation_id: str = Header("")):
    try:
        svid, obo = caller(authorization, x_svid, "tool-gateway")
    except jwt.PyJWTError as e:
        return denied("Tool Gateway", e)
    agent, bu = svid["agent"], svid["agent"].split("/")[0]
    steps = [step("Tool Gateway", "Verify caller", "pass", f"SVID {svid['sub']} and on-behalf-of token for {obo['sub']} (act matches).", "GR-01")]
    if tool not in TOOLS:
        steps.append(step("Tool Gateway", "Manifest scope", "deny", f"{tool} is not a registered tool.", "GR-02"))
        d = {"decision": "deny"}
    else:
        d = await opa(http, "controlplane/runtime/tool", {"agent": agent, "tool": tool, "user": obo["sub"], "scopes": obo["scope"].split()})
        steps += d["steps"]

    output = None
    if d["decision"] == "hold":
        r = await http.post(f"{CP_URL}/api/approvals", headers=SVC,
                            json={"agent": agent, "tool": tool, "user": obo["sub"], "args": req.args, "corr": x_correlation_id})
        case = r.json()["case"]
        steps[-1]["detail"] += f" Case {case}."
        output = f"Held for approval as {case}. Nothing was written."
    elif d["decision"] != "deny":
        output = f"{TOOLS[tool]['sys']} returned: {MOCK_SYSTEMS[tool](req.args)}"

    decision, key = summarize(steps)
    ev = await audit(kind="runtime", bu=bu, agent=agent, actor=obo["sub"], action=f"tool.call {tool}", decision=decision,
                     rule=key["rule"], detail=key["detail"], corr=x_correlation_id)
    return JSONResponse({"decision": decision, "steps": steps, "events": [ev], "output": output, "action": f"tool.call {tool}"},
                        status_code=403 if decision == "deny" else 200)
