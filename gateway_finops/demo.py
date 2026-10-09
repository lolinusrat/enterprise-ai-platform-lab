#!/usr/bin/env python3
"""Drive the LiteLLM gateway through routing, fallback, quotas and per-application cost tracking.

Standard library only. Run after `docker compose up -d`:  python3 demo.py
Each run creates three fresh "applications" (LiteLLM teams), each with its own virtual key,
so the cost report at the end covers this run only.
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:4000")
MASTER = os.environ.get("LITELLM_MASTER_KEY", "sk-master-demo")
RUN = time.strftime("%H%M%S")


def call(method, path, key, body=None):
    """Return (status, headers, json_body). Never raises on HTTP errors."""
    req = urllib.request.Request(
        GATEWAY + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw.decode(errors="replace")}
        return e.code, dict(e.headers), payload


def chat(key, model, prompt, **extra):
    body = {"model": model, "max_tokens": 200, "messages": [{"role": "user", "content": prompt}], **extra}
    return call("POST", "/v1/chat/completions", key, body)


def served_by(headers):
    return headers.get("x-litellm-model-id", "?")


def err(payload):
    e = payload.get("error", payload)
    return (e.get("message") if isinstance(e, dict) else str(e)).split("\n")[0][:140]


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ---------------------------------------------------------------------------------------------
section("0. Gateway health")
for _ in range(60):
    try:
        s, _, b = call("GET", "/health/readiness", MASTER)
        if s == 200:
            break
    except (urllib.error.URLError, ConnectionError):
        pass
    time.sleep(2)
else:
    sys.exit("gateway did not become ready on " + GATEWAY)
print(f"ready  status={b.get('status')}  db={b.get('db')}")

# ---------------------------------------------------------------------------------------------
section("1. Onboard three applications (team + virtual key each)")
APPS = {
    # name:             team quota                                    key quota
    "support-bot":     ({"max_budget": 1.00, "models": ["chat", "smart", "fast", "smart-outage"]}, {}),
    "analytics-batch": ({"max_budget": 0.0001, "models": ["smart"]}, {}),
    "internal-wiki":   ({"max_budget": 1.00, "models": ["fast"]}, {"rpm_limit": 2}),
}
keys, team_ids = {}, {}
for app, (team_q, key_q) in APPS.items():
    s, _, t = call("POST", "/team/new", MASTER, {"team_alias": f"{app}-{RUN}", **team_q})
    assert s == 200, (s, t)
    team_ids[app] = t["team_id"]
    s, _, k = call("POST", "/key/generate", MASTER,
                   {"team_id": t["team_id"], "key_alias": f"{app}-{RUN}", "metadata": {"app": app}, **key_q})
    assert s == 200, (s, k)
    keys[app] = k["key"]
    print(f"{app:16} team={t['team_id'][:8]}  models={team_q['models']}  "
          f"budget=${team_q['max_budget']}  rpm={key_q.get('rpm_limit', '-')}")

# ---------------------------------------------------------------------------------------------
section("2. Routing")
print("2a. Explicit tier routing - the app chooses by model name")
for model in ("fast", "smart"):
    s, h, b = chat(keys["support-bot"], model, "Name one prime number. One word.")
    print(f"  model={model:6} -> {served_by(h):20} status={s} cost=${float(h.get('x-litellm-response-cost', 0)):.8f}")

print("\n2b. Weighted load balancing - model 'chat' spans both endpoints (weights ollama 3 : groq 1)")
hits = Counter()
for i in range(20):
    s, h, b = chat(keys["support-bot"], "chat", f"Reply with the number {i}.")
    hits[served_by(h) if s == 200 else f"error {s}"] += 1
for dep, n in hits.most_common():
    print(f"  {dep:20} {n:2} / 20")

# ---------------------------------------------------------------------------------------------
section("3. Fallback")
print("Real upstream failure: 'smart-outage' points at Groq with bad credentials; fallback chain -> 'fast'")
for i in range(1, 3):
    s, h, b = chat(keys["support-bot"], "smart-outage", "Say OK.")
    print(f"  request {i}: status={s} served_by={served_by(h)} "
          f"attempted_fallbacks={h.get('x-litellm-attempted-fallbacks')}")

# ---------------------------------------------------------------------------------------------
section("4. Quotas")
print("4a. Model allow-list: internal-wiki may only use 'fast'")
s, h, b = chat(keys["internal-wiki"], "smart", "hi")
print(f"  model=smart -> status={s}  {err(b)}")

print("\n4b. Rate limit: internal-wiki key is capped at 2 requests/minute")
for i in range(1, 4):
    s, h, b = chat(keys["internal-wiki"], "fast", "hi")
    print(f"  request {i}: status={s}" + ("" if s == 200 else f"  {err(b)}"))

print("\n4c. Budget: analytics-batch team has a $0.0001 budget on the metered 'smart' tier")
for i in range(1, 9):
    s, h, b = chat(keys["analytics-batch"], "smart", "Write two sentences about data warehouses.")
    if s == 200:
        print(f"  request {i}: status=200 cost=${float(h.get('x-litellm-response-cost', 0)):.8f}")
    else:
        print(f"  request {i}: status={s}  {err(b)}")
        break
    time.sleep(1.5)  # spend is accounted asynchronously after each response

# ---------------------------------------------------------------------------------------------
section("5. Per-application cost report (read back from the gateway's spend ledger)")
time.sleep(6)  # let the batch writer flush spend logs to Postgres
by_app = defaultdict(lambda: {"requests": 0, "blocked": 0, "tokens": 0, "spend": 0.0, "models": Counter()})
inv = {v: k for k, v in team_ids.items()}
s, _, logs = call("GET", "/spend/logs?summarize=false", MASTER)
logs = logs if isinstance(logs, list) else logs.get("data", [])
for row in logs:
    app = inv.get(row.get("team_id"))
    if not app:
        continue
    a = by_app[app]
    if row.get("status") != "success":
        a["blocked"] += 1  # rejected by a quota or failed upstream; logged, never billed
        continue
    a["requests"] += 1
    a["tokens"] += row.get("total_tokens") or 0
    a["spend"] += row.get("spend") or 0.0
    a["models"][row.get("model_id") or row.get("model")] += 1

rows = []
print(f"{'application':16} {'served':>6} {'blocked':>7} {'tokens':>7} {'ledger $':>12} {'team $':>12} {'budget $':>9}  deployments")
for app, tid in team_ids.items():
    _, _, info = call("GET", f"/team/info?team_id={tid}", MASTER)
    team = info.get("team_info", {})
    a = by_app[app]
    deps = ", ".join(f"{m}x{n}" for m, n in a["models"].most_common())
    print(f"{app:16} {a['requests']:>6} {a['blocked']:>7} {a['tokens']:>7} {a['spend']:>12.8f} {team.get('spend', 0):>12.8f} "
          f"{team.get('max_budget')!s:>9}  {deps}")
    rows.append({"application": app, "team_id": tid, "requests": a["requests"], "blocked": a["blocked"], "tokens": a["tokens"],
                 "ledger_spend_usd": round(a["spend"], 10), "team_spend_usd": team.get("spend"),
                 "max_budget_usd": team.get("max_budget"), "deployments": deps})

out = f"cost_report_{RUN}.csv"
with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
print(f"\nwritten {out}")
