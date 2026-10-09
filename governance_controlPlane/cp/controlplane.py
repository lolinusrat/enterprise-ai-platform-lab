"""Control plane (port 8080): registry, admission, identity broker, budgets, audit ledger, UI.

It is not on the request path for policy decisions: it pushes the registry and catalog to
OPA (the gateways' local PDP) whenever they change, and the gateways decide locally.
"""
import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
import jwt as pyjwt
from fastapi import Depends, FastAPI, Form, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import reference as R
from .common import ATTESTATION, BROKER_ISS, GATEWAY_URL, IDP_ISS, LEDGER_DB, OPA_URL, SERVICE_TOKEN, opa
from .identity import OBO_TTL, SVID_TTL, USER_TTL, Signer
from .ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent

ledger = Ledger(LEDGER_DB, fresh=True)
idp = Signer("idp-1", IDP_ISS)
broker = Signer("broker-1", BROKER_ISS)
REG: dict[str, dict] = {}
USAGE = {k: v["used"] * 1_000_000 for k, v in R.BUS.items()}  # tokens used this month
BUNDLE = dict(R.BUNDLE_START)
APPROVALS: list[dict] = []
http: httpx.AsyncClient


class Manifest(BaseModel):
    bu: Literal["retail", "wealth", "people", "marketing"]
    name: str
    owner: str
    purpose: str = ""
    models: list[str] = []
    cls: int = Field(ge=0, le=3)
    tools: list[str] = []
    tier: Literal["low", "medium", "high"]
    evalScore: float | None = None
    hitl: bool = False
    customerFacing: bool = False
    tokens: int = Field(ge=0)


async def push() -> None:
    """Publish registry + catalog + usage to the PDP. Gateways enforce whatever was last pushed."""
    r = await http.put(f"{OPA_URL}/v1/data/cp", json=R.policy_data(REG, USAGE))
    r.raise_for_status()


async def admit(m: dict) -> dict:
    d = await opa(http, "controlplane/admission/decision", {"manifest": m})
    if d is None:
        raise HTTPException(400, "Admission policy returned no decision; check the manifest fields.")
    return d


def audit(**e) -> dict:
    return ledger.append(**e)


async def register(m: dict, d: dict, version: str = "1.0.0") -> dict:
    BUNDLE[m["bu"]] += 1
    a = {**m, "tier": d["tier"], "status": "pending" if d["outcome"] == "pending" else "active",
         "version": m.get("version", version), "bundle": f"{R.BUS[m['bu']]['code'].lower()}-{BUNDLE[m['bu']]}",
         "admitted": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    REG[R.agent_id(a)] = a
    warns = [c["id"] for c in d["checks"] if c["status"] == "warn"]
    audit(kind="admission", bu=a["bu"], agent=R.agent_id(a), actor=a["owner"].split("@")[0],
          action=f"deploy {a['version']}, tier {a['tier']}", decision=d["outcome"], rule="GR-09",
          detail=f"{len(d['checks'])} checks{', warnings ' + ', '.join(warns) if warns else ''}; bundle {a['bundle']}")
    await push()
    return a


async def set_status(aid: str, status: str, action: str, actor: str) -> dict:
    a = REG[aid]
    a["status"] = status
    audit(kind="control", bu=a["bu"], agent=aid, actor=actor, action=action,
          decision="reinstated" if status == "active" and action != "Risk Office sign-off" else status,
          rule="GR-01" if status == "suspended" else "GR-09", detail=f"{aid} is now {status}")
    await push()
    return a


async def seed() -> None:
    for _ in range(60):
        try:
            (await http.get(f"{OPA_URL}/health")).raise_for_status()
            break
        except httpx.HTTPError:
            await asyncio.sleep(0.5)
    else:
        raise RuntimeError(f"OPA is not reachable at {OPA_URL}. Start it with: docker compose up -d")
    await push()
    for s in R.SEED_AGENTS:
        d = await admit({k: v for k, v in s.items() if k != "version"})
        assert d["outcome"] != "rejected", (s["name"], d)
        await register(s, d)
    for aid, a in REG.items():
        if a["tier"] == "high":
            await set_status(aid, "active", "Risk Office sign-off", "risk-office")
    await set_status("marketing/seo-scout", "suspended", "kill switch: scraping a site whose terms forbid it", "marketing-admin")


@asynccontextmanager
async def lifespan(_):
    global http
    http = httpx.AsyncClient(timeout=10)
    await seed()
    yield
    await http.aclose()


app = FastAPI(title="Agent Control Plane", lifespan=lifespan)


# ---------- demo authn for admins and services ----------
def persona(x_persona: str = Header("platform")) -> str:
    """Stands in for an admin's SSO session. platform = governance team; otherwise a BU admin."""
    if x_persona not in R.PERSONAS:
        raise HTTPException(401, f"Unknown persona {x_persona}")
    return x_persona


def service(x_service_token: str = Header("")) -> None:
    if x_service_token != SERVICE_TOKEN:
        raise HTTPException(401, "Service token required")


def actor_name(p: str) -> str:
    return "risk-office" if p == "platform" else f"{p}-admin"


# ---------- reference and registry ----------
@app.get("/api/health")
async def health():
    return {"ok": True, "agents": len(REG), "events": len(ledger.events())}


@app.get("/api/reference")
async def reference():
    bus = {k: {**v, "used": round(USAGE[k] / 1_000_000, 1)} for k, v in R.BUS.items()}
    return {"classes": R.CLASSES, "tiers": R.TIERS, "models": R.MODELS, "tools": R.TOOLS, "bus": bus, "users": R.USERS,
            "guardrails": R.GUARDRAILS, "personas": R.PERSONAS, "presets": R.PRESETS, "drafts": R.DRAFTS, "gateway_url": GATEWAY_URL}


@app.get("/api/agents")
async def agents():
    return list(REG.values())


@app.get("/api/agents/{bu}/{name}")
async def agent(bu: str, name: str):
    a = REG.get(f"{bu}/{name}")
    if not a:
        raise HTTPException(404, "No such agent")
    return a


@app.post("/api/admission/dry-run")
async def dry_run(m: Manifest):
    return await admit(m.model_dump())


@app.post("/api/agents", status_code=201)
async def deploy(m: Manifest, p: str = Depends(persona)):
    if p not in ("platform", m.bu):
        raise HTTPException(403, f"{R.PERSONAS[p]} cannot deploy into {R.BUS[m.bu]['name']}")
    d = await admit(m.model_dump())
    if d["outcome"] == "rejected":
        audit(kind="admission", bu=m.bu, agent=f"{m.bu}/{m.name}", actor=m.owner.split("@")[0], action="deploy 1.0.0",
              decision="rejected", rule="GR-09", detail="failed: " + ", ".join(c["id"] for c in d["checks"] if c["status"] == "fail"))
        raise HTTPException(422, {"message": "Admission rejected", "decision": d})
    return {"agent": await register(m.model_dump(), d), "decision": d}


@app.post("/api/agents/{bu}/{name}/{action}")
async def control(bu: str, name: str, action: Literal["suspend", "reinstate", "approve", "reject"], p: str = Depends(persona)):
    aid = f"{bu}/{name}"
    a = REG.get(aid)
    if not a:
        raise HTTPException(404, "No such agent")
    if action in ("approve", "reject"):
        if p != "platform":
            raise HTTPException(403, "Only the Risk Office (platform governance) can approve or reject high-tier agents")
        if a["status"] != "pending":
            raise HTTPException(409, f"{aid} is {a['status']}, not pending")
        return await set_status(aid, "active" if action == "approve" else "rejected",
                                "Risk Office sign-off" if action == "approve" else "Risk Office rejected", actor_name(p))
    if p not in ("platform", bu):
        raise HTTPException(403, f"{R.PERSONAS[p]} cannot change {R.BUS[bu]['name']} agents")
    want = {"suspend": ("active", "suspended", "kill switch"), "reinstate": ("suspended", "active", "reinstated")}[action]
    if a["status"] != want[0]:
        raise HTTPException(409, f"{aid} is {a['status']}")
    return await set_status(aid, want[1], want[2], actor_name(p))


# ---------- identity: demo IdP + broker ----------
@app.get("/.well-known/jwks.json")
async def jwks():
    return {"keys": [idp.jwk(), broker.jwk()]}


@app.post("/idp/token")
async def idp_token(user: str = Form(...)):
    """Demo IdP: no password. In production the user signs in through Okta/Entra ID."""
    u = R.USERS.get(user)
    if not u:
        raise HTTPException(404, "Unknown user")
    tok = idp.sign({"sub": user, "aud": "agent-gateway", "bu": u["bu"], "role": u["role"], "groups": u["groups"], "amr": ["pwd", "mfa"]}, USER_TTL)
    return {"access_token": tok, "token_type": "Bearer", "expires_in": USER_TTL}


class SvidReq(BaseModel):
    agent: str


@app.post("/identity/svid")
async def svid(req: SvidReq, x_attestation: str = Header("")):
    """Stands in for the SPIRE agent's workload API: only active, registered agents get an SVID."""
    if x_attestation != ATTESTATION:
        raise HTTPException(401, "Node attestation failed")
    a = REG.get(req.agent)
    if not a or a["status"] != "active":
        raise HTTPException(403, f"No SVID: {req.agent} is {a['status'] if a else 'not registered'}")
    tok = broker.sign({"sub": R.spiffe(a), "agent": req.agent, "aud": ["identity-broker", "model-gateway", "tool-gateway"]}, SVID_TTL)
    return {"svid": tok, "spiffe_id": R.spiffe(a), "expires_in": SVID_TTL}


@app.post("/identity/token-exchange")
async def token_exchange(grant_type: str = Form(...), subject_token: str = Form(...), actor_token: str = Form(...),
                         subject_token_type: str = Form("urn:ietf:params:oauth:token-type:jwt"),
                         actor_token_type: str = Form("urn:ietf:params:oauth:token-type:jwt")):
    """RFC 8693 token exchange: user token + agent SVID -> on-behalf-of token scoped to agent ∩ user."""
    if grant_type != "urn:ietf:params:oauth:grant-type:token-exchange":
        raise HTTPException(400, {"error": "unsupported_grant_type"})
    try:
        user = pyjwt.decode(subject_token, idp.key.public_key(), algorithms=["RS256"], audience="agent-gateway", issuer=IDP_ISS)
        actor = pyjwt.decode(actor_token, broker.key.public_key(), algorithms=["RS256"], audience="identity-broker", issuer=BROKER_ISS)
    except pyjwt.PyJWTError as e:
        raise HTTPException(400, {"error": "invalid_grant", "error_description": str(e)})
    x = await opa(http, "controlplane/identity/exchange", {"agent": actor["agent"], "groups": user["groups"]})
    if not x or not x["allowed"]:
        raise HTTPException(403, {"error": "invalid_grant", "error_description": f"{actor['agent']} is not active"})
    tok = broker.sign({"sub": user["sub"], "act": {"sub": actor["sub"]}, "agent": actor["agent"], "bu": user["bu"],
                       "scope": " ".join(x["scopes"]), "aud": ["model-gateway", "tool-gateway"]}, OBO_TTL)
    return {"access_token": tok, "issued_token_type": "urn:ietf:params:oauth:token-type:access_token", "token_type": "Bearer",
            "expires_in": OBO_TTL, "scope": " ".join(x["scopes"]), "dropped": x["dropped"]}


# ---------- budgets and approvals (called by gateways) ----------
class Usage(BaseModel):
    bu: str
    agent: str
    tokens: int


@app.post("/api/usage", dependencies=[Depends(service)])
async def usage(u: Usage):
    USAGE[u.bu] += u.tokens
    await push()
    return {"bu": u.bu, "used_tokens": USAGE[u.bu]}


class Approval(BaseModel):
    agent: str
    tool: str
    user: str
    args: dict = {}
    corr: str = ""


@app.post("/api/approvals", dependencies=[Depends(service)])
async def new_approval(a: Approval):
    case = {"case": f"CASE-{1040 + len(APPROVALS) + 1}", **a.model_dump(), "status": "awaiting approver", "opened": int(time.time())}
    APPROVALS.append(case)
    return case


@app.get("/api/approvals")
async def approvals(p: str = Depends(persona)):
    return [c for c in APPROVALS if p == "platform" or c["agent"].startswith(p + "/")]


# ---------- audit ledger ----------
class Event(BaseModel):
    kind: str
    bu: str
    agent: str
    actor: str
    action: str
    decision: str
    rule: str
    detail: str
    corr: str = ""


@app.post("/api/audit", dependencies=[Depends(service)])
async def append_event(e: Event):
    ev = audit(**e.model_dump())
    return {"seq": ev["seq"], "hash": ev["hash"]}


@app.get("/api/audit")
async def events(p: str = Depends(persona)):
    return {"events": ledger.events(None if p == "platform" else p), "tampered": ledger.tampered()}


@app.get("/api/audit/verify")
async def verify():
    return ledger.verify()


@app.post("/api/audit/tamper")
async def tamper(p: str = Depends(persona)):
    """Demo only: rewrite the most recent denial as allowed, without fixing its hash."""
    if p != "platform":
        raise HTTPException(403, "Platform only")
    deny = [e for e in ledger.events() if e["decision"] == "deny"]
    if not deny:
        raise HTTPException(409, "No denied events to tamper with yet")
    ledger.tamper(deny[-1]["seq"], "allow")
    return {"tampered": deny[-1]["seq"]}


@app.post("/api/audit/untamper")
async def untamper(p: str = Depends(persona)):
    if p != "platform":
        raise HTTPException(403, "Platform only")
    ledger.untamper()
    return {"ok": True}


# ---------- UI ----------
@app.get("/")
async def ui():
    return FileResponse(ROOT / "index.html")
