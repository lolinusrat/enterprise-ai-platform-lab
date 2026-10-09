"""Demo UI: watch and drive the agent deployment on the local kind cluster.

    uv run --group ui uvicorn ui.server:app --port 8790      then open http://localhost:8790

Runs on the host and talks to the cluster with kubectl (context kind-agent-runtime), to the agent
through its NodePort, and to Jaeger through its query API.
"""
import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ui.loadgen import LoadGen

CONTEXT = "kind-agent-runtime"
NS = "agent"
AGENT_URL = "http://localhost:18080"
JAEGER_URL = "http://localhost:16687"
COLLECTOR_METRICS = "http://localhost:8889/metrics"
WORKERS = ["agent-runtime-worker", "agent-runtime-worker2"]


async def kubectl(*args: str, json_out: bool = True, stdin: str | None = None):
    cmd = ["kubectl", "--context", CONTEXT, *args] + (["-o", "json"] if json_out else [])
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE if stdin else None,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(stdin.encode() if stdin else None)
    if proc.returncode != 0:
        raise RuntimeError(err.decode().strip() or f"kubectl {' '.join(args)} failed")
    return json.loads(out) if json_out else out.decode()


def fp(token: str | None) -> str | None:
    return hashlib.sha256(token.encode()).hexdigest()[:8] if token else None


class Runtime:
    def __init__(self):
        self.token: str | None = None
        self.snapshot: dict = {}
        self.history: deque = deque(maxlen=400)           # ~13 min at 2s
        self.activity: deque = deque(maxlen=600)
        self.seen_events: set = set()
        self.rotation: dict | None = None
        self.load = LoadGen(AGENT_URL, lambda: self.token or "")

    def log(self, kind: str, text: str, source: str = "ui"):
        self.activity.appendleft({"t": time.time(), "kind": kind, "text": text, "source": source})

    async def read_token(self) -> str:
        sec = await kubectl("-n", NS, "get", "secret", "agent-secrets")
        self.token = base64.b64decode(sec["data"]["api-token"]).decode()
        return self.token


rt = Runtime()


# ---------------------------------------------------------------- sampling

def _cpu_milli(q: str) -> float:
    if q.endswith("n"):
        return int(q[:-1]) / 1e6
    if q.endswith("u"):
        return int(q[:-1]) / 1e3
    if q.endswith("m"):
        return float(q[:-1])
    return float(q) * 1000


def _pod_view(p: dict, usage: dict) -> dict:
    st = p.get("status", {})
    cs = (st.get("containerStatuses") or [{}])[0]
    conds = {c["type"]: c["status"] for c in st.get("conditions", [])}
    state = next(iter(cs.get("state", {}) or {}), "waiting")
    reason = (cs.get("state", {}).get(state) or {}).get("reason")
    if p["metadata"].get("deletionTimestamp"):
        phase = "Terminating"
    elif st.get("phase") == "Pending" and not p.get("spec", {}).get("nodeName"):
        phase = "Pending"
    elif conds.get("Ready") == "True":
        phase = "Ready"
    elif state == "running" and not cs.get("started"):
        phase = "Starting"
    else:
        phase = "NotReady"
    return {"name": p["metadata"]["name"], "node": p["spec"].get("nodeName"), "phase": phase,
            "restarts": cs.get("restartCount", 0), "reason": reason, "ip": st.get("podIP"),
            "created": p["metadata"]["creationTimestamp"], "cpu_m": usage.get(p["metadata"]["name"])}


async def sample() -> None:
    pods, hpa, nodes, events = await asyncio.gather(
        kubectl("-n", NS, "get", "pods", "-l", "app=agent-api"),
        kubectl("-n", NS, "get", "hpa", "agent-api"),
        kubectl("get", "nodes"),
        kubectl("-n", NS, "get", "events", "--sort-by=.lastTimestamp"),
    )
    try:
        raw = await kubectl("get", "--raw", f"/apis/metrics.k8s.io/v1beta1/namespaces/{NS}/pods", json_out=False)
        usage = {m["metadata"]["name"]: round(sum(_cpu_milli(c["usage"]["cpu"]) for c in m["containers"]))
                 for m in json.loads(raw)["items"]}
    except (RuntimeError, json.JSONDecodeError):
        usage = {}
    pod_views = sorted((_pod_view(p, usage) for p in pods["items"]), key=lambda x: x["created"])
    hs = hpa.get("status", {})
    cm = (hs.get("currentMetrics") or [{}])[0].get("resource", {}).get("current", {})
    target = hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"]
    node_views = [{"name": n["metadata"]["name"],
                   "role": "control-plane" if "node-role.kubernetes.io/control-plane" in n["metadata"]["labels"] else "worker",
                   "schedulable": not n["spec"].get("unschedulable", False),
                   "ready": any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])}
                  for n in nodes["items"]]
    for e in events["items"][-40:]:
        uid = e["metadata"]["uid"] + str(e.get("count", 1))
        if uid in rt.seen_events:
            continue
        rt.seen_events.add(uid)
        if rt.snapshot:  # don't replay history on first sample
            obj = e["involvedObject"]
            rt.log("warn" if e["type"] == "Warning" else "k8s",
                   f"{e['reason']}: {obj['kind']}/{obj['name']} - {e['message']}", source="kubernetes")
    w = rt.load.window()
    if "401" in w["errors"] and not (rt.rotation and rt.rotation["state"] == "propagating"):
        await rt.read_token()  # rotated elsewhere (demo.py, kubectl): pick up the current token
    rt.snapshot = {
        "pods": pod_views, "nodes": node_views,
        "hpa": {"current": hs.get("currentReplicas", 0), "desired": hs.get("desiredReplicas", 0),
                "min": hpa["spec"]["minReplicas"], "max": hpa["spec"]["maxReplicas"],
                "cpu_pct": cm.get("averageUtilization"), "target_pct": target},
        "load": w, "rotation": rt.rotation, "sampled_at": time.time(),
    }
    rt.history.append({"t": time.time(), "ready": sum(p["phase"] == "Ready" for p in pod_views),
                       "desired": hs.get("desiredReplicas", 0), "cpu_pct": cm.get("averageUtilization"),
                       "rps": w["rps"], "p95": w["p95_ms"], "err": w["error_rate"]})


async def sampler() -> None:
    while True:
        try:
            await sample()
        except Exception as e:  # cluster down or kubectl hiccup: keep the UI alive
            rt.snapshot = {**rt.snapshot, "error": str(e)[:300]}
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await rt.read_token()
    except RuntimeError as e:
        rt.log("warn", f"could not read agent-secrets: {e}")
    task = asyncio.create_task(sampler())
    yield
    task.cancel()
    rt.load.stop()


app = FastAPI(title="Kubernetes agent runtime demo", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/state")
def state():
    return {**rt.snapshot, "history": list(rt.history), "activity": list(rt.activity)[:60],
            "client_token_fp": fp(rt.token), "links": {"jaeger": JAEGER_URL, "agent": AGENT_URL}}


# ---------------------------------------------------------------- agent calls

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    backend: Literal["groq", "scripted"] = "scripted"


@app.post("/api/chat")
async def chat(req: ChatIn):
    if not rt.token:
        await rt.read_token()
    async with httpx.AsyncClient(base_url=AGENT_URL, timeout=60) as client:
        started = time.perf_counter()
        r = await client.post("/v1/chat", json=req.model_dump(), headers={"Authorization": f"Bearer {rt.token}"})
        if r.status_code == 401:  # token rotated elsewhere: re-read the Secret and retry once
            await rt.read_token()
            r = await client.post("/v1/chat", json=req.model_dump(), headers={"Authorization": f"Bearer {rt.token}"})
    body = r.json()
    rt.log("chat", f"{req.backend} chat -> {r.status_code} served by {r.headers.get('x-served-by')}")
    return {"status": r.status_code, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "served_by": r.headers.get("x-served-by"), "node": r.headers.get("x-node"), "body": body}


@app.get("/api/info")
async def info():
    async with httpx.AsyncClient(base_url=AGENT_URL, timeout=5) as client:
        return (await client.get("/v1/info")).json()


class LoadIn(BaseModel):
    rps: int = Field(ge=1, le=400)
    duration_s: int = Field(default=180, ge=10, le=900)


@app.post("/api/load")
async def start_load(req: LoadIn):
    if not rt.token:
        await rt.read_token()
    rt.load.start(req.rps, req.duration_s)
    rt.log("action", f"load started: {req.rps} req/s for {req.duration_s}s")
    return {"ok": True}


@app.delete("/api/load")
async def stop_load():
    rt.load.stop()
    rt.log("action", "load stopped")
    return {"ok": True}


# ---------------------------------------------------------------- chaos and operations

def _check_pod(name: str) -> None:
    if not any(p["name"] == name for p in rt.snapshot.get("pods", [])):
        raise HTTPException(404, "unknown pod")


@app.post("/api/pods/{name}/delete")
async def delete_pod(name: str):
    _check_pod(name)
    await kubectl("-n", NS, "delete", "pod", name, "--wait=false", json_out=False)
    rt.log("action", f"deleted pod {name}: the ReplicaSet replaces it")
    return {"ok": True}


class FaultIn(BaseModel):
    mode: Literal["none", "unready", "unhealthy"]


@app.post("/api/pods/{name}/fault")
async def fault(name: str, req: FaultIn):
    """Call /admin/fault on one specific pod (via exec, since the Service would pick a random pod)."""
    _check_pod(name)
    script = ("import urllib.request,sys,json;"
              "r=urllib.request.Request('http://127.0.0.1:8000/admin/fault',data=json.dumps({'mode':sys.argv[1]}).encode(),"
              "headers={'Authorization':'Bearer '+sys.stdin.read().strip(),'Content-Type':'application/json'});"
              "print(urllib.request.urlopen(r,timeout=5).read().decode())")
    await kubectl("-n", NS, "exec", "-i", name, "--", "python", "-c", script, req.mode,
                  json_out=False, stdin=rt.token)
    what = {"unready": "readiness now fails: removed from Service endpoints, not restarted",
            "unhealthy": "liveness now fails: the kubelet will restart the container",
            "none": "fault cleared"}[req.mode]
    rt.log("action", f"{name}: {what}")
    return {"ok": True}


@app.post("/api/rollout")
async def rollout():
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await kubectl("-n", NS, "patch", "deployment", "agent-api", "--type=merge", "-p",
                  json.dumps({"spec": {"template": {"metadata": {"annotations": {
                      "kubectl.kubernetes.io/restartedAt": stamp}}}}}), json_out=False)
    rt.log("action", "rolling update started (maxSurge 1, maxUnavailable 0)")
    return {"ok": True}


@app.post("/api/nodes/{name}/drain")
async def drain(name: str):
    if name not in WORKERS:
        raise HTTPException(400, "only worker nodes can be drained")
    rt.log("action", f"draining {name}: evictions respect the PodDisruptionBudget")
    asyncio.create_task(_drain(name))
    return {"ok": True}


async def _drain(name: str):
    try:
        await kubectl("drain", name, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=120s",
                      json_out=False)
        rt.log("action", f"{name} drained")
    except RuntimeError as e:
        rt.log("warn", f"drain {name}: {str(e)[:200]}")


@app.post("/api/nodes/{name}/uncordon")
async def uncordon(name: str):
    if name not in WORKERS:
        raise HTTPException(400, "unknown worker")
    await kubectl("uncordon", name, json_out=False)
    rt.log("action", f"{name} uncordoned: new pods may schedule there again")
    return {"ok": True}


# ---------------------------------------------------------------- secret rotation

@app.post("/api/rotate")
async def rotate():
    """Zero-downtime token rotation without restarting pods.

    1. Secret gets api-token=NEW and api-token-previous=OLD (pods accept both once synced).
    2. The kubelet swaps the mounted files into each pod over the next ~minute.
    3. This client keeps sending OLD until every pod reports NEW's fingerprint, then switches.
    """
    if rt.rotation and rt.rotation["state"] == "propagating":
        raise HTTPException(409, "a rotation is already in progress")
    old = await rt.read_token()
    new = secrets.token_hex(24)
    sec = await kubectl("-n", NS, "get", "secret", "agent-secrets")
    data = {**sec["data"], "api-token": base64.b64encode(new.encode()).decode(),
            "api-token-previous": base64.b64encode(old.encode()).decode()}
    await kubectl("-n", NS, "patch", "secret", "agent-secrets", "--type=merge", "-p",
                  json.dumps({"data": data}), json_out=False)
    rt.rotation = {"state": "propagating", "started": time.time(), "new_fp": fp(new), "old_fp": fp(old),
                   "pods_on_new": 0, "pods_total": 0}
    rt.log("action", f"secret rotated: new token {fp(new)} written, old {fp(old)} kept as previous")
    asyncio.create_task(_watch_rotation(new))
    return rt.rotation


async def _pod_token_fp(pod: str) -> str | None:
    script = ("import hashlib,pathlib;"
              "print(hashlib.sha256(pathlib.Path('/var/run/secrets/agent/api-token').read_text().strip().encode()).hexdigest()[:8])")
    try:
        return (await kubectl("-n", NS, "exec", pod, "--", "python", "-c", script, json_out=False)).strip()
    except RuntimeError:
        return None


async def _watch_rotation(new: str):
    deadline = time.time() + 240
    while time.time() < deadline:
        pods = [p["name"] for p in rt.snapshot.get("pods", []) if p["phase"] == "Ready"]
        fps = await asyncio.gather(*(_pod_token_fp(p) for p in pods))
        on_new = sum(f == fp(new) for f in fps)
        rt.rotation.update(pods_on_new=on_new, pods_total=len(pods))
        if pods and on_new == len(pods):
            rt.token = new
            rt.rotation.update(state="complete", seconds=round(time.time() - rt.rotation["started"]))
            rt.log("action", f"all {len(pods)} pods loaded token {fp(new)} after {rt.rotation['seconds']}s; "
                             "client switched, no restarts")
            return
        await asyncio.sleep(3)
    rt.rotation["state"] = "timed out"
    rt.log("warn", "rotation did not reach every pod within 4 minutes")


# ---------------------------------------------------------------- telemetry

@app.get("/api/traces")
async def traces(limit: int = 12):
    now = datetime.now(timezone.utc)
    params = {"query.service_name": "agent-api", "query.search_depth": limit,
              "query.start_time_min": (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
              "query.start_time_max": now.isoformat().replace("+00:00", "Z")}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{JAEGER_URL}/api/v3/traces", params=params)
        data = r.json().get("result", {}).get("resourceSpans", [])
    except (httpx.HTTPError, ValueError):
        return {"traces": [], "error": "Jaeger not reachable"}
    by_trace: dict[str, dict] = {}
    for rs in data:
        attrs = {a["key"]: next(iter(a["value"].values())) for a in rs["resource"]["attributes"]}
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                tid = base64.b64decode(s["traceId"]).hex() if len(s["traceId"]) != 32 else s["traceId"]
                t = by_trace.setdefault(tid, {"trace_id": tid, "spans": [], "pod": attrs.get("k8s.pod.name"),
                                               "node": attrs.get("k8s.node.name")})
                start, end = int(s["startTimeUnixNano"]), int(s["endTimeUnixNano"])
                t["spans"].append({"name": s["name"], "start": start, "ms": round((end - start) / 1e6, 2),
                                   "parent": bool(s.get("parentSpanId"))})
    out = []
    for t in by_trace.values():
        t["spans"].sort(key=lambda s: s["start"])
        root = next((s for s in t["spans"] if not s["parent"]), None)
        if root is None:  # root span not exported yet
            continue
        out.append({"trace_id": t["trace_id"], "root": root["name"], "ms": root["ms"], "start": root["start"],
                    "pod": t["pod"], "node": t["node"], "spans": [s["name"] for s in t["spans"]],
                    "url": f"{JAEGER_URL}/trace/{t['trace_id']}"})
    out.sort(key=lambda t: -t["start"])
    return {"traces": out[:limit]}


@app.get("/api/metrics")
async def metrics():
    """Per-pod counters as the OTel collector sees them (Prometheus scrape of every pod)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            text = (await client.get(COLLECTOR_METRICS)).text
    except httpx.HTTPError:
        return {"pods": {}, "error": "collector not reachable"}
    pods: dict[str, dict] = {}
    for line in text.splitlines():
        if not line.startswith(("agent_http_requests_total{", "agent_tool_calls_total{")):
            continue
        name, rest = line.split("{", 1)
        labels_s, value = rest.rsplit("} ", 1)
        labels = dict(kv.split("=", 1) for kv in labels_s.replace('"', "").split(",") if "=" in kv)
        pod = pods.setdefault(labels.get("pod", "?"), {"chat_200": 0, "chat_other": 0, "tools": 0})
        if name == "agent_http_requests_total" and labels.get("route") == "/v1/chat":
            pod["chat_200" if labels.get("status") == "200" else "chat_other"] += float(value)
        elif name == "agent_tool_calls_total":
            pod["tools"] += float(value)
    return {"pods": pods}
