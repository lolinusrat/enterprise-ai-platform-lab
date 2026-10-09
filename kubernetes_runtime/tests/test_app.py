"""Probe, auth and agent behaviour, run in-process with the scripted backend."""
import time

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    (tmp_path / "api-token").write_text(TOKEN)
    import agent_api.config as config
    monkeypatch.setattr(config.API_TOKEN, "path", tmp_path / "api-token")
    monkeypatch.setattr(config.API_TOKEN_PREVIOUS, "path", tmp_path / "api-token-previous")
    monkeypatch.setattr(config.GROQ_API_KEY, "path", tmp_path / "missing")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from agent_api import app as app_mod
    app_mod.state.warm, app_mod.state.draining, app_mod.state.fault = False, False, "none"
    with TestClient(app_mod.app) as c:
        deadline = time.time() + 10
        while not app_mod.state.warm and time.time() < deadline:
            time.sleep(0.05)
        yield c, app_mod, tmp_path


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_probes_pass_when_warm(client):
    c, _, _ = client
    assert c.get("/startupz").status_code == 200
    assert c.get("/livez").status_code == 200
    r = c.get("/readyz")
    assert r.status_code == 200 and r.json()["ready"]


def test_unready_fault_fails_readiness_only(client):
    c, _, _ = client
    assert c.post("/admin/fault", json={"mode": "unready"}, headers=AUTH).status_code == 200
    assert c.get("/readyz").status_code == 503
    assert c.get("/livez").status_code == 200


def test_unhealthy_fault_fails_liveness(client):
    c, _, _ = client
    c.post("/admin/fault", json={"mode": "unhealthy"}, headers=AUTH)
    assert c.get("/livez").status_code == 500


def test_draining_fails_readiness(client):
    c, app_mod, _ = client
    app_mod.state.draining = True
    assert c.get("/readyz").json()["checks"]["not_draining"] is False


def test_chat_requires_token(client):
    c, _, _ = client
    assert c.post("/v1/chat", json={"message": "status of A1001"}).status_code == 401
    assert c.post("/v1/chat", json={"message": "x"}, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_rotated_token_is_picked_up_without_restart(client):
    c, _, tmp = client
    (tmp / "api-token").write_text("rotated")
    import os
    os.utime(tmp / "api-token", (time.time() + 5, time.time() + 5))
    assert c.post("/v1/chat", json={"message": "status of A1001"}, headers=AUTH).status_code == 401
    ok = c.post("/v1/chat", json={"message": "status of A1001"}, headers={"Authorization": "Bearer rotated"})
    assert ok.status_code == 200


def test_scripted_agent_uses_tools(client):
    c, _, _ = client
    r = c.post("/v1/chat", json={"message": "What is the status and fraud risk of A1001?"}, headers=AUTH)
    body = r.json()
    assert r.status_code == 200
    assert body["tools"] == ["risk_score", "lookup_order"]
    assert "A1001 is shipped" in body["answer"] and "fraud risk" in body["answer"]
    assert r.headers["x-served-by"] == body["pod"]


def test_groq_rejected_when_not_configured(client):
    c, _, _ = client
    r = c.post("/v1/chat", json={"message": "hi", "backend": "groq"}, headers=AUTH)
    assert r.status_code == 400


def test_calculator_is_arithmetic_only():
    from agent_api.agent import calculate
    assert calculate("(2 + 3) * 4")["result"] == 20
    assert "error" in calculate("__import__('os')")


def test_metrics_exposed(client):
    c, _, _ = client
    c.post("/v1/chat", json={"message": "status of A1002"}, headers=AUTH)
    text = c.get("/metrics").text
    assert "agent_http_requests_total" in text and 'agent_tool_calls_total{tool="lookup_order"}' in text


def test_previous_token_accepted_during_rotation(client):
    c, _, tmp = client
    import os
    (tmp / "api-token").write_text("new-token")
    (tmp / "api-token-previous").write_text(TOKEN)
    os.utime(tmp / "api-token", (time.time() + 9, time.time() + 9))
    msg = {"message": "status of A1001"}
    assert c.post("/v1/chat", json=msg, headers=AUTH).status_code == 200
    assert c.post("/v1/chat", json=msg, headers={"Authorization": "Bearer new-token"}).status_code == 200
    from agent_api.config import fingerprint
    assert c.get("/v1/info").json()["token_fp"] == fingerprint("new-token")


def test_probes_are_not_traced_but_requests_are():
    from opentelemetry.sdk.trace.sampling import Decision
    from agent_api.telemetry import DropProbes
    s = DropProbes()
    assert s.should_sample(None, 1, "GET", attributes={"url.path": "/readyz"}).decision == Decision.DROP
    assert s.should_sample(None, 1, "POST", attributes={"url.path": "/v1/chat"}).decision != Decision.DROP
