"""Authorization server and tool gateway.

The agent reaches customer data only through ToolGateway.invoke. The gateway is the policy enforcement point:
it asks OPA about every call, and it is the only component that can read the records store. Who the user is,
which agent is calling and which session this is come from the authenticated Session, never from the model.
"""
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from secagent import records
from secagent.keys import SigningKey
from secagent.opa import OPA, OPAUnavailable
from secagent.reference import AUDIENCE, ISSUER, policy_data


@dataclass
class Session:
    user: str
    agent: str
    id: str = field(default_factory=lambda: "s-" + uuid.uuid4().hex[:8])


class Trace:
    """Ordered hops between components, rendered by the UI as a sequence diagram."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def add(self, src: str, dst: str, label: str, status: str = "info", **detail) -> None:
        self.events.append({"step": len(self.events) + 1, "from": src, "to": dst, "label": label,
                            "status": status, "detail": detail})


class AuditLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, **event) -> None:
        self.events.append({"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **event})


class AuthorizationServer:
    """Issues short-lived, signed grants. Asks OPA (custauthz.grant) whether a grant may be issued."""

    def __init__(self, opa: OPA, key: SigningKey, audit: AuditLog) -> None:
        self.opa, self.key, self.audit = opa, key, audit

    def request(self, session: Session, customer_id: str, purpose: str, reference: str, trace: Trace) -> dict:
        query = {"user": session.user, "agent": session.agent, "customer_id": customer_id,
                 "purpose": purpose, "reference": reference}
        trace.add("authz", "opa", "POST custauthz/grant/decision", input=query)
        try:
            decision, ms = self.opa.decide("custauthz/grant/decision", query)
        except OPAUnavailable as e:
            decision, ms = {"allow": False, "deny": [{"id": "OPA", "msg": f"policy engine unavailable: {e}"}]}, 0.0
        trace.add("opa", "authz", "allow" if decision["allow"] else "deny",
                  "allow" if decision["allow"] else "deny", result=decision, latency_ms=round(ms, 1))
        self.audit.record(component="authorization-server", action="issue_grant", session=session.id,
                          user=session.user, agent=session.agent, customer_id=customer_id, purpose=purpose,
                          allow=decision["allow"], reasons=[r["id"] for r in decision["deny"]])
        if not decision["allow"]:
            return {"status": "denied", "reasons": decision["deny"]}
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": session.user, "sid": session.id, "azp": session.agent,
                  "customer_id": customer_id, "purpose": purpose, "ref": reference,
                  "jti": uuid.uuid4().hex[:12], "iat": now, "exp": now + decision["ttl_seconds"]}
        return {"status": "granted", "grant": self.key.sign(claims), "claims": claims,
                "expires_in": decision["ttl_seconds"], "fields": decision["fields"]}


class ToolGateway:
    """Policy enforcement point for every tool call. Fails closed if OPA cannot be reached."""

    def __init__(self, opa: OPA, authz: AuthorizationServer, audit: AuditLog) -> None:
        self.opa, self.authz, self.audit = opa, authz, audit

    def invoke(self, session: Session, tool: str, args: dict, trace: Trace) -> dict:
        query = {"user": session.user, "agent": session.agent, "session": session.id, "tool": tool, "args": args}
        trace.add("gateway", "opa", "POST custauthz/access/decision", input=query)
        try:
            decision, ms = self.opa.decide("custauthz/access/decision", query)
        except OPAUnavailable as e:
            decision, ms = {"allow": False, "deny": [{"id": "OPA", "msg": f"policy engine unavailable: {e}"}],
                            "fields": [], "grant_claims": None}, 0.0
        trace.add("opa", "gateway", "allow" if decision["allow"] else "deny",
                  "allow" if decision["allow"] else "deny", result=decision, latency_ms=round(ms, 1))

        released: list[str] = []
        allowed, reasons = decision["allow"], decision["deny"]
        if not decision["allow"]:
            result = {"error": "denied by policy", "reasons": decision["deny"]}
        elif tool == "find_customer":
            trace.add("gateway", "records", f"search name={args.get('name', '')!r}")
            matches = [_project(r, decision["fields"]) for r in records.find(str(args.get("name", "")))]
            trace.add("records", "gateway", f"{len(matches)} match(es)", fields=decision["fields"])
            result = {"matches": matches}
        elif tool == "request_authorization":
            trace.add("gateway", "authz", "request grant", customer_id=args.get("customer_id"),
                      purpose=args.get("purpose"), reference=args.get("reference", ""))
            result = self.authz.request(session, str(args.get("customer_id", "")), str(args.get("purpose", "")),
                                        str(args.get("reference", "") or ""), trace)
            ok = result["status"] == "granted"
            allowed, reasons = ok, result.get("reasons", [])
            trace.add("authz", "gateway", "grant issued" if ok else "grant refused", "allow" if ok else "deny",
                      claims=result.get("claims"), reasons=result.get("reasons"))
            result = {k: v for k, v in result.items() if k != "claims"}
        elif tool == "get_customer_record":
            record = records.get(args["customer_id"])
            trace.add("gateway", "records", f"read {args['customer_id']}")
            withheld = sorted(set(record or {}) - set(decision["fields"]))
            trace.add("records", "gateway", f"{len(decision['fields'])} fields released, {len(withheld)} withheld",
                      fields=decision["fields"], withheld=withheld)
            result = {"record": _project(record, decision["fields"]), "withheld_by_policy": withheld}
            released = [args["customer_id"]]
        else:  # allowed by policy but not implemented here
            result = {"error": f"tool {tool} is not implemented"}

        self.audit.record(component="tool-gateway", action=tool, session=session.id, user=session.user,
                          agent=session.agent, customer_id=args.get("customer_id"), allow=allowed,
                          reasons=[r["id"] for r in reasons], fields_released=decision["fields"] if released else [])
        return {"result": result, "allow": allowed, "reasons": reasons, "decision": decision, "released": released}


def _project(record: dict | None, fields: list[str]) -> dict | None:
    return None if record is None else {k: record[k] for k in fields if k in record}


def bootstrap(opa: OPA) -> tuple[ToolGateway, AuthorizationServer, AuditLog, SigningKey]:
    """Generate the signing key and push policy data (with the public JWKS) to OPA."""
    key = SigningKey()
    opa.put_data("secagent", policy_data(key.jwks))
    audit = AuditLog()
    authz = AuthorizationServer(opa, key, audit)
    return ToolGateway(opa, authz, audit), authz, audit, key


def as_tool_message(outcome: dict) -> str:
    return json.dumps(outcome["result"])
