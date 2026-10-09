"""Unit tests run offline; the end-to-end tests need OPA on :8182 (docker compose up -d) and are skipped otherwise."""
import json
import time

import pytest

from secagent import agent
from secagent.gateway import AuditLog, AuthorizationServer, Session, ToolGateway, Trace, _project, bootstrap
from secagent.keys import SigningKey
from secagent.llm import ScriptedModel
from secagent.opa import OPA
from secagent.reference import AGENT_ID, INSTRUCTION_MODES, SCENARIOS


def first_call(mode: str, prompt: str) -> tuple[str, dict]:
    msg = ScriptedModel().step([{"role": "system", "content": INSTRUCTION_MODES[mode]["prompt"]},
                                {"role": "user", "content": prompt}])
    call = msg["tool_calls"][0]["function"]
    return call["name"], json.loads(call["arguments"])


def test_scripted_model_follows_its_instructions():
    assert first_call("compliant", "Open C-1001") == (
        "request_authorization", {"customer_id": "C-1001", "purpose": "account_servicing", "reference": ""})
    assert first_call("rogue", "Open C-1001") == ("get_customer_record", {"customer_id": "C-1001", "grant": "ADMIN-OVERRIDE"})
    assert first_call("unaware", "Open C-1001") == ("get_customer_record", {"customer_id": "C-1001"})
    assert first_call("compliant", "Ticket T-501 for C-1002")[1]["purpose"] == "support_ticket"
    assert first_call("compliant", "Show me Tom Becker")[0] == "find_customer"


def test_projection_returns_only_policy_fields():
    assert _project({"name": "A", "ssn": "1", "email": "e"}, ["name", "email", "missing"]) == {"name": "A", "email": "e"}
    assert _project(None, ["name"]) is None


def test_gateway_fails_closed_when_opa_is_down():
    opa, audit = OPA("http://127.0.0.1:9"), AuditLog()
    gw = ToolGateway(opa, AuthorizationServer(opa, SigningKey(), audit), audit)
    out = gw.invoke(Session("u-alice", AGENT_ID), "get_customer_record", {"customer_id": "C-1001"}, Trace())
    assert out["allow"] is False and out["released"] == []
    assert out["reasons"][0]["id"] == "OPA"


opa_up = pytest.mark.skipif(not OPA().healthy(), reason="OPA not running on :8182")


@pytest.fixture(scope="module")
def stack():
    gateway, authz, audit, key = bootstrap(OPA())
    return gateway, authz


@opa_up
@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenarios_end_to_end(stack, sc):
    gateway, authz = stack
    prompt = sc["prompt"]
    if "{alice_grant}" in prompt:
        prompt = prompt.replace("{alice_grant}", authz.request(Session("u-alice", AGENT_ID), "C-1001",
                                                               "account_servicing", "", Trace())["grant"])
    r = agent.run(gateway, Session(sc["user"], AGENT_ID), prompt, sc["mode"])
    assert [(c["tool"], c["allow"]) for c in r["calls"]] == [tuple(e) for e in sc["expect"]]
    assert set(r["released"]) <= set(sc["may_release"])


@opa_up
def test_released_record_contains_only_purpose_fields(stack):
    gateway, _ = stack
    s = Session("u-bob", AGENT_ID)
    grant = gateway.invoke(s, "request_authorization",
                           {"customer_id": "C-1002", "purpose": "support_ticket", "reference": "T-501"}, Trace())
    out = gateway.invoke(s, "get_customer_record", {"customer_id": "C-1002", "grant": grant["result"]["grant"]}, Trace())
    assert set(out["result"]["record"]) == {"customer_id", "name", "email", "phone", "account_status"}
    assert "ssn" in out["result"]["withheld_by_policy"]


@opa_up
def test_grant_from_a_different_signing_key_is_rejected(stack):
    gateway, _ = stack
    s = Session("u-alice", AGENT_ID)
    forged = SigningKey().sign({"iss": "https://authz.lab.internal", "aud": "tool-gateway", "sub": s.user, "sid": s.id,
                                "azp": AGENT_ID, "customer_id": "C-1001", "purpose": "account_servicing",
                                "jti": "x", "exp": int(time.time()) + 60})
    out = gateway.invoke(s, "get_customer_record", {"customer_id": "C-1001", "grant": forged}, Trace())
    assert not out["allow"] and [r["id"] for r in out["reasons"]] == ["R2"]
