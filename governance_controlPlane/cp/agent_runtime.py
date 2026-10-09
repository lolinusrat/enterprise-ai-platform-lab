"""BU agent runtime (port 8082): stands in for the BU tenant namespaces.

Holds no model keys or tool credentials. For each run it fetches its own SVID from the
identity broker, exchanges the user's token for an on-behalf-of token, and calls models and
tools only through the gateways. Only the agent gateway may call it (network policy in
production; a shared header here).
"""
import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .common import ATTESTATION, CP_URL, GATEWAY_TOKEN, GATEWAY_URL, step

app = FastAPI(title="BU agent runtime")
http = httpx.AsyncClient(timeout=180)


class RunReq(BaseModel):
    kind: str = "model"
    model: str | None = None
    cls: int = 1
    prompt: str = ""
    tool: str | None = None
    args: dict = {}


@app.post("/run/{bu}/{name}")
async def run(bu: str, name: str, req: RunReq, authorization: str = Header(""), x_correlation_id: str = Header(""),
              x_gateway_token: str = Header("")):
    if x_gateway_token != GATEWAY_TOKEN:
        raise HTTPException(403, "Only the agent gateway may call agents")
    agent = f"{bu}/{name}"
    cfg = (await http.get(f"{CP_URL}/api/agents/{agent}")).json()

    r = await http.post(f"{CP_URL}/identity/svid", json={"agent": agent}, headers={"X-Attestation": ATTESTATION})
    if r.status_code != 200:
        return {"steps": [step("Identity Broker", "Fetch SVID", "deny", r.json()["detail"], "GR-01")], "events": []}
    svid = r.json()["svid"]

    r = await http.post(f"{CP_URL}/identity/token-exchange", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": authorization.removeprefix("Bearer "), "actor_token": svid})
    if r.status_code != 200:
        return {"steps": [step("Identity Broker", "On-behalf-of token exchange", "deny", str(r.json()["detail"]), "GR-05")], "events": []}
    x = r.json()
    dropped = f" Dropped because the user lacks them: {', '.join(x['dropped'])}." if x["dropped"] else ""
    steps = [step("Identity Broker", "On-behalf-of token exchange", "pass", f"Scopes granted: {x['scope'] or 'none'}.{dropped}", "GR-05")]

    headers = {"Authorization": f"Bearer {x['access_token']}", "X-SVID": svid, "X-Correlation-Id": x_correlation_id}
    if req.kind == "model":
        messages = [{"role": "system", "content": f"You are {name}, an assistant that {cfg['purpose'].lower()}. Reply in under 80 words."},
                    {"role": "user", "content": req.prompt}]
        r = await http.post(f"{GATEWAY_URL}/v1/model", json={"model": req.model, "cls": req.cls, "messages": messages}, headers=headers)
    else:
        r = await http.post(f"{GATEWAY_URL}/v1/tools/{req.tool}", json={"args": req.args}, headers=headers)
    body = r.json()
    return {"steps": steps + body.get("steps", []), "events": body.get("events", []), "output": body.get("output"),
            "served_model": body.get("served_model"), "action": body.get("action")}
