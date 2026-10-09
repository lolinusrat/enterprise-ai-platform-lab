"""End-to-end run against the live services. Start them first (see README), then:

    uv run python demo.py

Exits non-zero if any decision differs from what the design says it should be.
"""
import sys
import time

import httpx

from cp.reference import DRAFTS, PRESETS

CP, GW = "http://localhost:8080", "http://localhost:8081"
http = httpx.Client(timeout=300)
failures = []


def expect(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: expected {want}, got {got}")
    return "ok " if ok else "XX "


def as_persona(p):
    return {"X-Persona": p}


def token(user):
    return http.post(f"{CP}/idp/token", data={"user": user}).json()["access_token"]


def invoke(p):
    bu, name = p["agent"].split("/")
    body = {k: p[k] for k in ("kind", "model", "cls", "tool") if k in p} | {"prompt": p.get("prompt", ""), "args": p.get("args", {})}
    return http.post(f"{GW}/agents/{bu}/{name}/invoke", json=body, headers={"Authorization": f"Bearer {token(p['user'])}"}).json()


def section(t):
    print(f"\n== {t} " + "=" * (70 - len(t)))


section("1. Admission (deploy-time, Rego in OPA)")
for d in DRAFTS:
    r = http.post(f"{CP}/api/agents", json=d["d"], headers=as_persona(d["d"]["bu"]))
    dec = r.json()["decision"] if r.status_code == 201 else r.json()["detail"]["decision"]
    flagged = [f"{c['id']}:{c['status']}" for c in dec["checks"] if c["status"] != "pass"]
    print(f"{expect(d['label'], dec['outcome'], d['expect'])}{d['label']:<26} {dec['outcome']:<9} HTTP {r.status_code}  {' '.join(flagged)}")
r = http.post(f"{CP}/api/agents/retail/collections-agent/approve", headers=as_persona("retail"))
print(f"{expect('BU admin approval', r.status_code, 403)}Retail admin tries to approve own high-tier agent -> HTTP {r.status_code}")
r = http.post(f"{CP}/api/agents/retail/collections-agent/approve", headers=as_persona("platform"))
print(f"{expect('Risk Office approval', r.json().get('status'), 'active')}Risk Office approves collections-agent -> {r.json().get('status')}")

section("2. Runtime scenarios (gateways -> OPA -> Ollama / mock systems)")
for p in PRESETS:
    t0 = time.perf_counter()
    r = invoke(p)
    print(f"{expect(p['label'], r['decision'], p['expect'])}{p['label']:<32} {r['decision']:<12} {r['key']['rule']}  "
          f"{(time.perf_counter() - t0):5.1f}s  {r.get('served_model') or ''}")
    print(f"      {r['key']['detail']}")
    if r.get("output"):
        print("      output: " + r["output"].replace("\n", " ")[:220])

section("3. Kill switch")
r = http.post(f"{CP}/api/agents/retail/branch-assist/suspend", headers=as_persona("marketing"))
print(f"{expect('cross-BU suspend', r.status_code, 403)}Marketing admin tries to suspend a Retail agent -> HTTP {r.status_code}")
http.post(f"{CP}/api/agents/retail/branch-assist/suspend", headers=as_persona("retail"))
r = invoke(PRESETS[0] | {"prompt": "Draft a short thank-you note."})
print(f"{expect('suspended call', r['decision'], 'deny')}Retail admin suspends branch-assist; next call -> {r['decision']} ({r['key']['rule']})")
http.post(f"{CP}/api/agents/retail/branch-assist/reinstate", headers=as_persona("retail"))
r = invoke(PRESETS[0] | {"prompt": "Draft a short thank-you note."})
print(f"{expect('reinstated call', r['decision'], 'allow')}Reinstated; next call -> {r['decision']}")

section("4. Tokens: forged and wrong-audience tokens are refused")
r = http.post(f"{GW}/agents/retail/branch-assist/invoke", json={"prompt": "hi", "model": "claude-haiku-4-5"},
              headers={"Authorization": "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0."})
print(f"{expect('forged token', r.json()['decision'], 'deny')}Unsigned user token -> {r.json()['decision']}: {r.json()['key']['detail'][:70]}")
r = http.post(f"{GW}/v1/model", json={"model": "gpt-oss-120b", "cls": 3, "messages": [{"role": "user", "content": "x"}]},
              headers={"Authorization": f"Bearer {token('m.rossi')}"})
print(f"{expect('direct model call', r.status_code, 401)}Calling the model gateway directly with a user token, no SVID -> HTTP {r.status_code}")

section("5. Audit ledger")
v = http.get(f"{CP}/api/audit/verify").json()
print(f"{expect('chain intact', v['ok'], True)}Chain verified over {v['n']} events, head {v.get('head', '')[:16]}")
t = http.post(f"{CP}/api/audit/tamper", headers=as_persona("platform")).json()["tampered"]
v = http.get(f"{CP}/api/audit/verify").json()
print(f"{expect('tamper detected', v.get('seq'), t)}Rewrote event #{t} from deny to allow -> verify reports break at #{v.get('seq')}")
http.post(f"{CP}/api/audit/untamper", headers=as_persona("platform"))
print(f"{expect('restored', http.get(f'{CP}/api/audit/verify').json()['ok'], True)}Restored -> chain intact again")
ev = http.get(f"{CP}/api/audit", headers=as_persona("retail")).json()["events"]
print(f"{expect('tenant view', {e['bu'] for e in ev}, {'retail'})}Retail admin's audit view holds {len(ev)} events, all Retail")
print(f"Pending approvals: {[c['case'] + ' ' + c['tool'] for c in http.get(f'{CP}/api/approvals').json()]}")

print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES:'}")
for f in failures:
    print("  " + f)
sys.exit(1 if failures else 0)
