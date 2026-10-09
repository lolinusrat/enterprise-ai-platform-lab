"""Agent API: the workload deployed to Kubernetes.

    uvicorn agent_api.app:app --host 0.0.0.0 --port 8000

Probes (wired to the Deployment's startupProbe / readinessProbe / livenessProbe):
  /startupz  passes once warm-up has finished; until then the kubelet holds the other probes off
  /readyz    passes when warm, a token is mounted, the pod is not draining and no fault is injected;
             failing it removes the pod from Service endpoints without restarting it
  /livez     fails only when the process is wedged (simulated by the "unhealthy" fault);
             failing it makes the kubelet restart the container
Readiness deliberately ignores the upstream LLM: a Groq outage should degrade answers, not pull
every replica out of the Service at once.
"""
import hmac
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from .agent import run_agent
from .config import API_TOKEN, API_TOKEN_PREVIOUS, GROQ_API_KEY, fingerprint, settings
from .telemetry import INFLIGHT, LATENCY, NOISE as PROBES, READY, REQUESTS, setup_logging, setup_tracing, shutdown_tracing

log = setup_logging()


class State:
    started_at = time.time()
    warm = False
    draining = False
    fault: str = "none"   # none | unready | unhealthy


state = State()


def _warm_up() -> None:
    time.sleep(settings.startup_delay_s)
    state.warm = True
    log.info("warm-up complete", extra={"fields": {"startup_s": settings.startup_delay_s}})


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_tracing()
    threading.Thread(target=_warm_up, daemon=True).start()
    log.info("starting", extra={"fields": {"node": settings.node_name, "version": settings.version}})
    yield
    # SIGTERM: Kubernetes has already started removing this pod from endpoints (and preStop slept
    # while that propagated). Fail readiness, let uvicorn finish in-flight requests, flush spans.
    state.draining = True
    log.info("draining")
    shutdown_tracing()


app = FastAPI(title="Agent API", version=settings.version, lifespan=lifespan)


@app.middleware("http")
async def observe(request: Request, call_next):
    """RED metrics and the served-by headers. FastAPI itself opens the HTTP server span
    (and the sampler drops it for probes), so the agent's spans nest under it."""
    path = request.url.path
    route = path if path in PROBES or path.startswith(("/v1/", "/admin/")) else "other"
    started = time.perf_counter()
    counted = path not in PROBES
    if counted:
        INFLIGHT.inc()
    try:
        response = await call_next(request)
    finally:
        if counted:
            INFLIGHT.dec()
    REQUESTS.labels(route, str(response.status_code)).inc()
    LATENCY.labels(route).observe(time.perf_counter() - started)
    response.headers["x-served-by"] = settings.pod_name
    response.headers["x-node"] = settings.node_name
    return response


def require_token(authorization: str = Header(default="")) -> None:
    expected = API_TOKEN.get()
    if not expected:
        raise HTTPException(503, "API token not mounted")
    supplied = authorization.removeprefix("Bearer ").strip().encode()
    accepted = [t for t in (expected, API_TOKEN_PREVIOUS.get()) if t]
    if not any(hmac.compare_digest(supplied, t.encode()) for t in accepted):
        raise HTTPException(401, "invalid token")


# ---------------------------------------------------------------- probes

@app.get("/startupz")
def startupz(response: Response):
    if not state.warm:
        response.status_code = 503
    return {"warm": state.warm, "uptime_s": round(time.time() - state.started_at, 1)}


@app.get("/readyz")
def readyz(response: Response):
    checks = {"warm": state.warm, "token_mounted": API_TOKEN.get() is not None,
              "not_draining": not state.draining, "no_fault": state.fault != "unready"}
    ok = all(checks.values())
    READY.set(1 if ok else 0)
    if not ok:
        response.status_code = 503
    return {"ready": ok, "checks": checks, "pod": settings.pod_name}


@app.get("/livez")
def livez(response: Response):
    if state.fault == "unhealthy":
        response.status_code = 500
        return {"alive": False, "reason": "injected fault"}
    return {"alive": True}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------- API

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    backend: Literal["groq", "scripted"] | None = None


@app.get("/v1/info")
def info():
    backends = ["groq", "scripted"] if GROQ_API_KEY.get() else ["scripted"]
    return {"pod": settings.pod_name, "node": settings.node_name, "namespace": settings.namespace,
            "version": settings.version, "backends": backends, "default_backend": settings.default_backend,
            "fault": state.fault, "uptime_s": round(time.time() - state.started_at, 1),
            "token_fp": fingerprint(API_TOKEN.get())}


@app.post("/v1/chat", dependencies=[Depends(require_token)])
def chat(req: ChatRequest):
    backend = req.backend or settings.default_backend
    if backend == "groq" and not GROQ_API_KEY.get():
        raise HTTPException(400, "groq backend is not configured on this deployment")
    try:
        out = run_agent(req.message, backend)
    except Exception as e:  # upstream model errors surface as 502, not as a crashed pod
        log.exception("agent failed")
        raise HTTPException(502, f"agent error: {type(e).__name__}") from e
    log.info("chat", extra={"fields": {"backend": backend, "tools": out["tools"], "agent_ms": out["agent_ms"]}})
    return {**out, "pod": settings.pod_name, "node": settings.node_name}


class FaultRequest(BaseModel):
    mode: Literal["none", "unready", "unhealthy"]


@app.post("/admin/fault", dependencies=[Depends(require_token)])
def inject_fault(req: FaultRequest):
    """Demo only: make this pod fail readiness (traffic drained) or liveness (container restarted)."""
    state.fault = req.mode
    log.warning("fault injected", extra={"fields": {"mode": req.mode}})
    return {"pod": settings.pod_name, "fault": state.fault}
