"""Reference data the platform team owns: catalog, tools, BU policies, demo users, seed agents.

The same values are embedded in index.html for the offline simulation; when the UI is
served by the control plane it loads them from /api/reference instead.
"""
import json
from pathlib import Path

CLASSES = ["Public", "Internal", "Confidential", "Restricted"]
TIERS = ["low", "medium", "high"]

# rate = illustrative chargeback in USD per 1M tokens, not a quoted price
MODELS = {
    "claude-opus-5-5": {"provider": "Anthropic (private endpoint)", "hosting": "external", "maxClass": 2, "rate": 15, "status": "active"},
    "claude-sonnet-5-5": {"provider": "Anthropic (private endpoint)", "hosting": "external", "maxClass": 2, "rate": 3, "status": "active"},
    "claude-haiku-4-5": {"provider": "Anthropic (private endpoint)", "hosting": "external", "maxClass": 2, "rate": 1, "status": "active"},
    "gpt-oss-120b": {"provider": "self-hosted GPU pool, ap-southeast-2", "hosting": "in-boundary", "maxClass": 3, "rate": 0.4, "status": "active"},
    "legacy-chat-v1": {"provider": "public SaaS API", "hosting": "external", "maxClass": 1, "rate": 2, "status": "deprecated"},
}

TOOLS = {
    "crm.read": {"sys": "CRM", "mode": "read", "cls": 2, "ent": "crm-reader"},
    "crm.write": {"sys": "CRM", "mode": "write", "cls": 2, "ent": "crm-writer"},
    "core-banking.read": {"sys": "Core banking", "mode": "read", "cls": 3, "ent": "cb-reader"},
    "payments.refund": {"sys": "Payments", "mode": "write", "cls": 3, "ent": "refund-approver"},
    "portfolio.read": {"sys": "Portfolio", "mode": "read", "cls": 3, "ent": "wealth-advisor"},
    "research.search": {"sys": "Research", "mode": "read", "cls": 1, "ent": "all-staff"},
    "hris.read": {"sys": "HRIS", "mode": "read", "cls": 3, "ent": "hr-partner"},
    "policy-kb.search": {"sys": "Policy KB", "mode": "read", "cls": 1, "ent": "all-staff"},
    "cms.publish": {"sys": "CMS", "mode": "write", "cls": 0, "ent": "content-publisher"},
    "analytics.read": {"sys": "Analytics", "mode": "read", "cls": 1, "ent": "marketing-analyst"},
    "web.search": {"sys": "Web", "mode": "read", "cls": 0, "ent": "all-staff"},
}

# budget and used are millions of tokens per month
BUS = {
    "retail": {"name": "Retail Banking", "code": "RB", "models": ["claude-sonnet-5-5", "claude-haiku-4-5", "gpt-oss-120b"], "maxClass": 3,
               "tools": ["crm.read", "crm.write", "core-banking.read", "payments.refund", "policy-kb.search"], "budget": 400, "used": 236},
    "wealth": {"name": "Wealth Management", "code": "WM", "models": ["claude-opus-5-5", "claude-sonnet-5-5", "gpt-oss-120b"], "maxClass": 3,
               "tools": ["crm.read", "crm.write", "portfolio.read", "research.search"], "budget": 250, "used": 118},
    "people": {"name": "People & Culture", "code": "PC", "models": ["gpt-oss-120b", "claude-haiku-4-5"], "maxClass": 3,
               "tools": ["hris.read", "policy-kb.search"], "budget": 80, "used": 22},
    "marketing": {"name": "Marketing", "code": "MK", "models": ["claude-sonnet-5-5", "claude-haiku-4-5"], "maxClass": 1,
                  "tools": ["cms.publish", "analytics.read", "web.search"], "budget": 150, "used": 131},
}
BUNDLE_START = {"retail": 14, "wealth": 9, "people": 4, "marketing": 7}

# The demo IdP: in production these come from Okta/Entra ID group claims.
USERS = {
    "j.okafor": {"bu": "retail", "role": "Branch officer", "groups": ["all-staff", "crm-reader", "cb-reader"]},
    "m.rossi": {"bu": "retail", "role": "Disputes lead", "groups": ["all-staff", "crm-reader", "crm-writer", "cb-reader", "refund-approver"]},
    "t.adeyemi": {"bu": "retail", "role": "Contractor", "groups": ["all-staff"]},
    "s.patel": {"bu": "wealth", "role": "Associate adviser", "groups": ["all-staff", "crm-reader", "wealth-advisor"]},
    "l.nguyen": {"bu": "people", "role": "HR business partner", "groups": ["all-staff", "hr-partner"]},
    "k.berg": {"bu": "marketing", "role": "Content lead", "groups": ["all-staff", "content-publisher", "marketing-analyst"]},
}

GUARDRAILS = [
    ["GR-01", "Every agent has a named owner and its own workload identity (SPIFFE ID). No shared keys.", "Admission · Agent Gateway"],
    ["GR-02", "Agents call only active models from the enterprise catalog that their BU is entitled to.", "Admission · Model Gateway"],
    ["GR-03", "Data never reaches a model or tool cleared for a lower classification.", "Admission · Model Gateway · Tool Gateway"],
    ["GR-04", "Personal data is redacted before a prompt leaves the network boundary.", "Model Gateway"],
    ["GR-05", "An agent never acts with more access than the person it serves (on-behalf-of token).", "Identity Broker · Tool Gateway"],
    ["GR-06", "Write actions by high-risk agents wait for a named human approver.", "Admission · Tool Gateway"],
    ["GR-07", "Token spend stays inside the BU's monthly allocation.", "Admission · Model Gateway"],
    ["GR-08", "Every decision, allowed or denied, goes to the tamper-evident audit ledger.", "All enforcement points"],
    ["GR-09", "Medium and high-risk agents carry passing eval evidence; high risk needs Risk Office sign-off.", "Admission"],
    ["GR-10", "An agent serves its own BU's users unless it is published enterprise-wide.", "Agent Gateway"],
]

SEED_AGENTS = [
    {"bu": "retail", "name": "branch-assist", "owner": "j.okafor@corp.example", "purpose": "Drafts replies to customer emails for branch staff",
     "models": ["claude-haiku-4-5", "claude-sonnet-5-5"], "cls": 2, "tools": ["crm.read", "policy-kb.search"], "tier": "medium",
     "evalScore": 0.93, "hitl": False, "customerFacing": False, "tokens": 40, "version": "1.4.0"},
    {"bu": "retail", "name": "dispute-resolver", "owner": "m.rossi@corp.example", "purpose": "Investigates card disputes and proposes refunds",
     "models": ["claude-sonnet-5-5", "gpt-oss-120b"], "cls": 3, "tools": ["core-banking.read", "crm.read", "payments.refund"], "tier": "high",
     "evalScore": 0.95, "hitl": True, "customerFacing": False, "tokens": 60, "version": "2.1.0"},
    {"bu": "wealth", "name": "advisor-copilot", "owner": "s.patel@corp.example", "purpose": "Prepares client review packs and CRM notes",
     "models": ["claude-opus-5-5", "gpt-oss-120b"], "cls": 3, "tools": ["portfolio.read", "crm.read", "crm.write", "research.search"], "tier": "high",
     "evalScore": 0.94, "hitl": True, "customerFacing": False, "tokens": 50, "version": "3.0.2"},
    {"bu": "people", "name": "hr-helpdesk", "owner": "l.nguyen@corp.example", "purpose": "Answers leave and policy questions for employees",
     "models": ["gpt-oss-120b"], "cls": 3, "tools": ["hris.read", "policy-kb.search"], "tier": "medium",
     "evalScore": 0.9, "hitl": False, "customerFacing": False, "tokens": 15, "version": "1.0.3"},
    {"bu": "marketing", "name": "campaign-writer", "owner": "k.berg@corp.example", "purpose": "Drafts and publishes campaign landing copy",
     "models": ["claude-sonnet-5-5", "claude-haiku-4-5"], "cls": 1, "tools": ["cms.publish", "analytics.read", "web.search"], "tier": "medium",
     "evalScore": 0.88, "hitl": False, "customerFacing": True, "tokens": 30, "version": "0.9.0"},
    {"bu": "marketing", "name": "seo-scout", "owner": "k.berg@corp.example", "purpose": "Scans competitor pages for keyword gaps",
     "models": ["claude-haiku-4-5"], "cls": 0, "tools": ["web.search"], "tier": "low",
     "evalScore": None, "hitl": False, "customerFacing": False, "tokens": 5, "version": "0.3.1"},
]

# Example manifests for the Deploy tab and demo.py
DRAFTS = [
    {"label": "Mortgage pre-qualifier", "expect": "admitted",
     "d": {"bu": "retail", "name": "mortgage-prequal", "owner": "priya.shah@corp.example", "purpose": "Checks mortgage pre-qualification against serviceability rules",
           "models": ["claude-sonnet-5-5", "gpt-oss-120b"], "cls": 3, "tools": ["core-banking.read", "crm.read"], "tier": "medium",
           "evalScore": 0.91, "hitl": False, "customerFacing": False, "tokens": 35}},
    {"label": "Collections agent", "expect": "pending",
     "d": {"bu": "retail", "name": "collections-agent", "owner": "m.rossi@corp.example", "purpose": "Arranges hardship payment plans with customers",
           "models": ["gpt-oss-120b"], "cls": 3, "tools": ["core-banking.read", "crm.write"], "tier": "high",
           "evalScore": 0.92, "hitl": True, "customerFacing": True, "tokens": 45}},
    {"label": "Campaign agent on Opus", "expect": "rejected",
     "d": {"bu": "marketing", "name": "brand-voice", "owner": "k.berg@corp.example", "purpose": "Rewrites partner copy in house style",
           "models": ["claude-opus-5-5", "legacy-chat-v1"], "cls": 2, "tools": ["cms.publish", "web.search"], "tier": "medium",
           "evalScore": 0.9, "hitl": False, "customerFacing": False, "tokens": 20}},
]

# Runtime scenarios for the Runtime tab and demo.py
PRESETS = [
    {"label": "Branch email draft", "note": "allowed, PII redacted", "expect": "obligations", "agent": "retail/branch-assist", "user": "j.okafor",
     "kind": "model", "model": "claude-sonnet-5-5", "cls": 2, "pii": True, "tool": "crm.read",
     "prompt": "Draft a reply to this customer email: \"Hi, I'm Sarah Mitchell. My card 4111 1111 1111 1111 was charged twice for the same purchase. Call me on 0412 345 678 or email sarah.mitchell@example.com.\""},
    {"label": "Restricted prompt", "note": "rerouted in-boundary", "expect": "obligations", "agent": "retail/dispute-resolver", "user": "m.rossi",
     "kind": "model", "model": "claude-sonnet-5-5", "cls": 3, "pii": True, "tool": "core-banking.read",
     "prompt": "Summarise the dispute history for account 062-000 12345678 held by Mr David Chen and say whether a refund is justified."},
    {"label": "Refund by high-risk agent", "note": "held for approval", "expect": "hold", "agent": "retail/dispute-resolver", "user": "m.rossi",
     "kind": "tool", "model": "gpt-oss-120b", "cls": 3, "pii": False, "tool": "payments.refund", "args": {"account": "062-000 12345678", "amount": 89.95}},
    {"label": "CRM write, user lacks rights", "note": "denied, on-behalf-of", "expect": "deny", "agent": "wealth/advisor-copilot", "user": "s.patel",
     "kind": "tool", "model": "gpt-oss-120b", "cls": 2, "pii": False, "tool": "crm.write", "args": {"client": "C-20931", "note": "Annual review booked"}},
    {"label": "Confidential brief to Marketing", "note": "denied, data class", "expect": "deny", "agent": "marketing/campaign-writer", "user": "k.berg",
     "kind": "model", "model": "claude-sonnet-5-5", "cls": 2, "pii": False, "tool": "web.search",
     "prompt": "Write landing page copy from this confidential Q4 pricing brief: home loan rate cut of 0.35% from 1 November."},
    {"label": "Suspended agent", "note": "denied, kill switch", "expect": "deny", "agent": "marketing/seo-scout", "user": "k.berg",
     "kind": "model", "model": "claude-haiku-4-5", "cls": 0, "pii": False, "tool": "web.search",
     "prompt": "List keyword gaps for home loan pages."},
    {"label": "Cross-BU call", "note": "denied, audience", "expect": "deny", "agent": "people/hr-helpdesk", "user": "j.okafor",
     "kind": "model", "model": "gpt-oss-120b", "cls": 1, "pii": False, "tool": "policy-kb.search",
     "prompt": "How many days of annual leave do I have left?"},
]

PERSONAS = {"platform": "Platform governance (all BUs)", "retail": "Retail Banking admin", "wealth": "Wealth Management admin",
            "people": "People & Culture admin", "marketing": "Marketing admin"}


def agent_id(a: dict) -> str:
    return f"{a['bu']}/{a['name']}"


def spiffe(a: dict) -> str:
    return f"spiffe://corp.example/bu/{a['bu']}/agent/{a['name']}"


def policy_data(agents: dict, usage: dict) -> dict:
    """The document pushed to every PDP under data.cp. usage is tokens used this month per BU."""
    keep = ("bu", "name", "owner", "models", "cls", "tools", "tier", "hitl", "customerFacing", "tokens", "status", "bundle", "version")
    return {
        "models": MODELS,
        "tools": TOOLS,
        "bus": {k: {f: v[f] for f in ("name", "code", "models", "maxClass", "tools", "budget")} for k, v in BUS.items()},
        "agents": {i: {f: a.get(f) for f in keep} for i, a in agents.items()},
        "usage": usage,
    }


def seed_policy_data() -> dict:
    """Policy data as it stands after seeding; written to policies/testdata for `opa test`."""
    agents = {}
    for s in SEED_AGENTS:
        a = {**s, "status": "suspended" if s["name"] == "seo-scout" else "active", "bundle": f"{BUS[s['bu']]['code'].lower()}-test"}
        agents[agent_id(a)] = a
    return policy_data(agents, {k: v["used"] * 1_000_000 for k, v in BUS.items()})


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "policies" / "testdata" / "cp" / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(seed_policy_data(), indent=1) + "\n")
    print(f"wrote {out}")
