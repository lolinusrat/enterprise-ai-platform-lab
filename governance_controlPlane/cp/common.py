"""Settings and helpers shared by the control plane, gateways and agent runtime."""
import json
import os

import httpx

CP_URL = os.getenv("CP_URL", "http://localhost:8080")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8081")
RUNTIME_URL = os.getenv("RUNTIME_URL", "http://localhost:8082")
OPA_URL = os.getenv("OPA_URL", "http://localhost:8181")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# ollama: call Ollama, fall back to the mock if it is unreachable. mock: never call a model.
MODEL_BACKEND = os.getenv("MODEL_BACKEND", "ollama")
# Catalog model -> local Ollama model that stands in for it in this demo.
DEFAULT_STANDIN = os.getenv("MODEL_STANDIN", "llama3.2:1b")
STANDINS = json.loads(os.getenv("MODEL_STANDINS", "{}"))

# Demo shared secrets. In production: mTLS between services and SPIRE node attestation.
SERVICE_TOKEN = os.getenv("SERVICE_TOKEN", "svc-demo-token")          # gateways -> control plane (audit, usage)
ATTESTATION = os.getenv("RUNTIME_ATTESTATION", "node-attest-demo")   # agent runtime -> identity broker
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "gw-demo-token")           # agent gateway -> agent runtime

LEDGER_DB = os.getenv("LEDGER_DB", "ledger.sqlite3")

IDP_ISS = "https://idp.corp.example"
BROKER_ISS = "https://identity.corp.example"


def step(comp, check, status, detail, rule):
    return {"comp": comp, "check": check, "status": status, "detail": detail, "rule": rule}


def summarize(steps):
    """Same precedence as lib.decision in Rego: deny > hold > obligations > allow."""
    for status, decision in (("deny", "deny"), ("hold", "hold"), ("warn", "obligations")):
        hit = next((s for s in steps if s["status"] == status), None)
        if hit:
            return decision, hit
    return "allow", steps[-1] if steps else None


async def opa(client: httpx.AsyncClient, path: str, input_: dict):
    r = await client.post(f"{OPA_URL}/v1/data/{path}", json={"input": input_})
    r.raise_for_status()
    return r.json().get("result")
