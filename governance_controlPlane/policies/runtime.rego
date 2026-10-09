# Request-time decisions evaluated by the gateways' local PDP.
#   data.controlplane.runtime.invoke  input: {"agent", "user": {"sub","bu","role","groups"}}
#   data.controlplane.runtime.model   input: {"agent", "model", "cls", "declared_cls", "pii_entities": [...]}
#   data.controlplane.runtime.tool    input: {"agent", "tool", "user", "scopes": [...]}
package controlplane.runtime

import data.controlplane.lib

classes := lib.classes

agent := data.cp.agents[input.agent]

bu := data.cp.bus[agent.bu]

models := data.cp.models

tools := data.cp.tools

spiffe := sprintf("spiffe://corp.example/bu/%s/agent/%s", [agent.bu, agent.name])

# ---------------- Agent Gateway: invoke ----------------
inv_status := lib.step("Agent Gateway", "Authenticate agent", "pass", sprintf("%s is active; trusted SVID subject %s; policy bundle %s loaded.", [input.agent, spiffe, agent.bundle]), "GR-01") if {
	agent.status == "active"
} else := lib.step("Agent Gateway", "Authenticate agent", "deny", sprintf("%s is suspended. Its SVID is revoked, so the gateway refuses the session.", [input.agent]), "GR-01") if {
	agent.status == "suspended"
} else := lib.step("Agent Gateway", "Authenticate agent", "deny", sprintf("%s is %s; no SVID has been issued.", [input.agent, agent.status]), "GR-09") if {
	agent
} else := lib.step("Agent Gateway", "Authenticate agent", "deny", sprintf("%s is not in the registry.", [input.agent]), "GR-01")

inv_audience := lib.step("Policy Decision Point", "Audience", "pass", sprintf("%s (%s) is in %s, the agent's audience.", [input.user.sub, input.user.role, bu.name]), "GR-10") if {
	input.user.bu == agent.bu
} else := lib.step("Policy Decision Point", "Audience", "deny", sprintf("%s is in %s; %s serves %s only.", [input.user.sub, data.cp.bus[input.user.bu].name, input.agent, bu.name]), "GR-10") if {
	agent
} else := lib.step("Policy Decision Point", "Audience", "deny", "Unknown agent or business unit.", "GR-10")

invoke := {"steps": steps, "decision": lib.decision(steps)} if {
	steps := lib.upto_deny([inv_status, inv_audience])
}

# ---------------- Model Gateway ----------------
model_entitled := lib.step("Model Gateway", "Model entitlement", "deny", sprintf("%s is not in %s's manifest.", [input.model, input.agent]), "GR-02") if {
	not input.model in agent.models
} else := lib.step("Model Gateway", "Model entitlement", "deny", sprintf("%s is not available to %s.", [input.model, bu.name]), "GR-02") if {
	not input.model in bu.models
} else := lib.step("Model Gateway", "Model entitlement", "deny", sprintf("%s is not active in the catalog.", [input.model]), "GR-02") if {
	not models[input.model].status == "active"
} else := lib.step("Model Gateway", "Model entitlement", "pass", sprintf("%s is in the manifest, the BU entitlement and the active catalog.", [input.model]), "GR-02")

raised := sprintf(" Raised from %s because personal data was detected.", [classes[input.declared_cls]]) if {
	input.cls > input.declared_cls
} else := ""

alt := [x |
	some x in agent.models
	models[x].maxClass >= input.cls
	x in bu.models
	models[x].status == "active"
]

route := input.model if {
	models[input.model].maxClass >= input.cls
} else := alt[0] if {
	count(alt) > 0
}

model_class := lib.step("Model Gateway", "Data classification", "deny", sprintf("Prompt classified %s; the agent's ceiling is %s.%s", [classes[input.cls], classes[agent.cls], raised]), "GR-03") if {
	input.cls > agent.cls
} else := lib.step("Model Gateway", "Data classification", "pass", sprintf("%s prompt is within the agent ceiling and %s's clearance.%s", [classes[input.cls], input.model, raised]), "GR-03") if {
	route == input.model
} else := lib.step("Model Gateway", "Data classification", "warn", sprintf("%s is cleared to %s. Rerouted to %s (%s).%s", [input.model, classes[models[input.model].maxClass], route, models[route].hosting, raised]), "GR-03") if {
	route
} else := lib.step("Model Gateway", "Data classification", "deny", sprintf("%s is cleared to %s and the agent has no model cleared for %s.%s", [input.model, classes[models[input.model].maxClass], classes[input.cls], raised]), "GR-03")

external if models[route].hosting == "external"

redact if {
	external
	count(input.pii_entities) > 0
}

pii_kinds := lib.join(sort({e | some e in input.pii_entities}))

model_pii := lib.step("Model Gateway", "PII redaction", "warn", sprintf("Redacted %v entities (%s) before sending to %s.", [count(input.pii_entities), pii_kinds, models[route].provider]), "GR-04") if {
	redact
} else := lib.step("Model Gateway", "PII redaction", "pass", sprintf("%s runs inside the boundary; personal data (%s) may stay in the prompt at this class.", [route, pii_kinds]), "GR-04") if {
	count(input.pii_entities) > 0
	route
} else := lib.step("Model Gateway", "PII redaction", "pass", "No personal data detected.", "GR-04") if {
	count(input.pii_entities) == 0
} else := lib.step("Model Gateway", "PII redaction", "skip", "Not evaluated.", "GR-04")

used_m := round((data.cp.usage[agent.bu] / 1000000) * 10) / 10

pct := used_m / bu.budget

model_budget := lib.step("Model Gateway", "Budget", "deny", sprintf("%s has used its %vM token allocation.", [bu.name, bu.budget]), "GR-07") if {
	pct >= 1
} else := lib.step("Model Gateway", "Budget", "warn", sprintf("%vM of %vM tokens used (%v%%). BU admin alerted at 85%%.", [used_m, bu.budget, round(pct * 100)]), "GR-07") if {
	pct >= 0.85
} else := lib.step("Model Gateway", "Budget", "pass", sprintf("%vM of %vM tokens used (%v%%). Spend metered to %s.", [used_m, bu.budget, round(pct * 100), input.agent]), "GR-07")

route_out := route if {
	route
} else := null

redact_out := true if {
	redact
} else := false

model := {"steps": steps, "decision": lib.decision(steps), "route": route_out, "redact": redact_out} if {
	steps := lib.upto_deny([model_entitled, model_class, model_pii, model_budget])
}

# ---------------- Tool Gateway ----------------
t := tools[input.tool]

tool_manifest := lib.step("Tool Gateway", "Manifest scope", "deny", sprintf("%s is not declared in %s's manifest.", [input.tool, input.agent]), "GR-02") if {
	not input.tool in agent.tools
} else := lib.step("Tool Gateway", "Manifest scope", "pass", sprintf("%s is declared in the manifest.", [input.tool]), "GR-02")

tool_scope := lib.step("Tool Gateway", "On-behalf-of scope", "deny", sprintf("%s is not in group %s, so the token carries no %s scope. The agent cannot do what its user cannot.", [input.user, t.ent, input.tool]), "GR-05") if {
	not input.tool in input.scopes
} else := lib.step("Tool Gateway", "On-behalf-of scope", "pass", sprintf("%s holds %s; token carries %s.", [input.user, t.ent, input.tool]), "GR-05")

tool_class := lib.step("Tool Gateway", "Data classification", "deny", sprintf("%s returns %s data, above the %s ceiling.", [t.sys, classes[t.cls], classes[agent.cls]]), "GR-03") if {
	t.cls > agent.cls
} else := lib.step("Tool Gateway", "Data classification", "pass", sprintf("%s returns %s data, within the %s ceiling.", [t.sys, classes[t.cls], classes[agent.cls]]), "GR-03")

needs_approval if agent.tier == "high"

needs_approval if agent.hitl == true

tool_oversight := lib.step("Tool Gateway", "Human oversight", "hold", sprintf("Write to %s queued for a second approver in the %s case queue. The requester cannot approve it.", [t.sys, bu.name]), "GR-06") if {
	t.mode == "write"
	needs_approval
} else := lib.step("Tool Gateway", "Human oversight", "pass", sprintf("Write allowed unattended at %s tier.", [agent.tier]), "GR-06") if {
	t.mode == "write"
} else := lib.step("Tool Gateway", "Human oversight", "pass", "Read-only call.", "GR-06")

tool := {"steps": steps, "decision": lib.decision(steps), "mode": t.mode} if {
	steps := lib.upto_deny([tool_manifest, tool_scope, tool_class, tool_oversight])
}
