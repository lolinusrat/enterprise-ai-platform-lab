# Deploy-time checks A1-A10. Query: data.controlplane.admission.decision
# input: {"manifest": {...}}; data.cp is the current registry and catalog pushed by the control plane.
package controlplane.admission

import data.controlplane.lib

m := input.manifest

bu := data.cp.bus[m.bu]

models := data.cp.models

tools := data.cp.tools

classes := lib.classes

agent_id := sprintf("%s/%s", [m.bu, m.name])

spiffe := sprintf("spiffe://corp.example/bu/%s/agent/%s", [m.bu, m.name])

rank := {"low": 0, "medium": 1, "high": 2}

chk(id, label, status, detail, rule) := {"id": id, "label": label, "status": status, "detail": detail, "rule": rule}

# A1 ownership and identity
a1 := chk("A1", "Ownership & identity", "fail", "Agent name must be 3 to 31 lowercase letters, digits or hyphens.", "GR-01") if {
	not regex.match(`^[a-z][a-z0-9-]{2,30}$`, m.name)
} else := chk("A1", "Ownership & identity", "fail", sprintf("%s is already registered. Pick another name.", [agent_id]), "GR-01") if {
	data.cp.agents[agent_id]
} else := chk("A1", "Ownership & identity", "fail", "Owner must be a named corp.example mailbox, not a team alias or blank.", "GR-01") if {
	not regex.match(`^[^@\s]+@corp\.example$`, m.owner)
} else := chk("A1", "Ownership & identity", "pass", sprintf("Owner %s. Identity on admission: %s", [m.owner, spiffe]), "GR-01")

# A2 model catalog
unknown_models := [x | some x in m.models; not models[x]]

deprecated := [x | some x in m.models; models[x].status != "active"]

a2 := chk("A2", "Model catalog", "fail", "Choose at least one model.", "GR-02") if {
	count(m.models) == 0
} else := chk("A2", "Model catalog", "fail", sprintf("%s is not in the enterprise catalog.", [lib.join(unknown_models)]), "GR-02") if {
	count(unknown_models) > 0
} else := chk("A2", "Model catalog", "fail", sprintf("%s is deprecated in the enterprise catalog. New deployments cannot use it.", [lib.join(deprecated)]), "GR-02") if {
	count(deprecated) > 0
} else := chk("A2", "Model catalog", "pass", "Every model is active in the enterprise catalog.", "GR-02")

# A3 BU entitlement: a BU policy narrows the catalog, never widens it
not_entitled := [x | some x in m.models; not x in bu.models]

a3 := chk("A3", "BU model entitlement", "fail", sprintf("%s is not entitled to %s. A BU policy narrows the catalog; it cannot widen it.", [bu.name, lib.join(not_entitled)]), "GR-02") if {
	count(not_entitled) > 0
} else := chk("A3", "BU model entitlement", "fail", "No models chosen.", "GR-02") if {
	count(m.models) == 0
} else := chk("A3", "BU model entitlement", "pass", sprintf("All models are on %s's entitlement list.", [bu.name]), "GR-02")

# A4 data clearance
cleared := [x | some x in m.models; models[x].maxClass >= m.cls]

below := [x | some x in m.models; models[x].maxClass < m.cls]

a4 := chk("A4", "Data clearance", "fail", sprintf("%s caps agents at %s; this manifest declares %s.", [bu.name, classes[bu.maxClass], classes[m.cls]]), "GR-03") if {
	m.cls > bu.maxClass
} else := chk("A4", "Data clearance", "fail", "No models to check.", "GR-03") if {
	count(m.models) == 0
} else := chk("A4", "Data clearance", "fail", sprintf("No chosen model is cleared for %s data. Add an in-boundary model such as gpt-oss-120b.", [classes[m.cls]]), "GR-03") if {
	count(cleared) == 0
} else := chk("A4", "Data clearance", "warn", sprintf("%s is cleared only to %s. The gateway will route %s prompts to %s.", [lib.join(below), classes[max([models[x].maxClass | some x in below])], classes[m.cls], lib.join(cleared)]), "GR-03") if {
	count(below) > 0
} else := chk("A4", "Data clearance", "pass", sprintf("Every model is cleared for %s data.", [classes[m.cls]]), "GR-03")

# A5 tool scope
not_approved := [t | some t in m.tools; not t in bu.tools]

over_class := [t | some t in m.tools; tools[t].cls > m.cls]

a5 := chk("A5", "Tool scope", "fail", sprintf("%s is not approved for %s.", [lib.join(not_approved), bu.name]), "GR-03") if {
	count(not_approved) > 0
} else := chk("A5", "Tool scope", "fail", sprintf("%s returns %s data, above the agent's %s ceiling.", [lib.join(over_class), classes[max([tools[t].cls | some t in over_class])], classes[m.cls]]), "GR-03") if {
	count(over_class) > 0
} else := chk("A5", "Tool scope", "pass", sprintf("%v tool(s), all approved for %s and within the data ceiling.", [count(m.tools), bu.name]), "GR-03") if {
	count(m.tools) > 0
} else := chk("A5", "Tool scope", "pass", "No tools. Model-only agent.", "GR-03")

# A6 risk tier: derived from the manifest; the declared tier may be higher, never lower
writes := [t | some t in m.tools; tools[t].mode == "write"]

cls_why := ["Restricted data +2"] if {
	m.cls == 3
} else := ["Confidential data +1"] if {
	m.cls == 2
} else := []

cls_pts := 2 if {
	m.cls == 3
} else := 1 if {
	m.cls == 2
} else := 0

write_why := ["write tools +1"] if {
	count(writes) > 0
} else := []

rwrite_why := ["writes to Restricted systems +1"] if {
	some t in writes
	tools[t].cls == 3
} else := []

cf_why := ["customer-facing +1"] if {
	m.customerFacing == true
} else := []

reasons := array.concat(array.concat(cls_why, write_why), array.concat(rwrite_why, cf_why))

score := ((cls_pts + count(write_why)) + count(rwrite_why)) + count(cf_why)

derived := "high" if {
	score >= 3
} else := "medium" if {
	score >= 1
} else := "low"

why := lib.join(reasons) if {
	count(reasons) > 0
} else := "no risk factors"

tier := derived if {
	rank[derived] > rank[m.tier]
} else := m.tier

a6 := chk("A6", "Risk tier", "fail", sprintf("Declared %s, but the manifest scores %v (%s), which is %s.", [m.tier, score, why, derived]), "GR-09") if {
	rank[m.tier] < rank[derived]
} else := chk("A6", "Risk tier", "pass", sprintf("Declared %s; derived %s (score %v: %s).", [m.tier, derived, score, why]), "GR-09")

# A7 evaluation evidence
need := 0.9 if {
	tier == "high"
} else := 0.85

a7 := chk("A7", "Evaluation evidence", "pass", "Not required at low tier.", "GR-09") if {
	tier == "low"
} else := chk("A7", "Evaluation evidence", "fail", sprintf("%s tier needs an enterprise-safety-v3 score of at least %v; got none.", [tier, need]), "GR-09") if {
	not is_number(m.evalScore)
} else := chk("A7", "Evaluation evidence", "fail", sprintf("%s tier needs an enterprise-safety-v3 score of at least %v; got %v.", [tier, need, m.evalScore]), "GR-09") if {
	m.evalScore < need
} else := chk("A7", "Evaluation evidence", "pass", sprintf("enterprise-safety-v3 score %v meets the %v bar for %s tier.", [m.evalScore, need, tier]), "GR-09")

# A8 human oversight
a8 := chk("A8", "Human oversight", "fail", sprintf("High-risk agent with write tools (%s) must send writes to a human approver.", [lib.join(writes)]), "GR-06") if {
	tier == "high"
	count(writes) > 0
	not m.hitl
} else := chk("A8", "Human oversight", "pass", sprintf("Writes (%s) go to a human approver.", [lib.join(writes)]), "GR-06") if {
	count(writes) > 0
	m.hitl
} else := chk("A8", "Human oversight", "pass", sprintf("Writes (%s) run unattended, which %s tier allows.", [lib.join(writes), tier]), "GR-06") if {
	count(writes) > 0
} else := chk("A8", "Human oversight", "pass", "No write tools.", "GR-06")

# A9 budget allocation from the BU's monthly cap
allocated := sum([a.tokens | some a in data.cp.agents; a.bu == m.bu; a.status != "rejected"])

free := bu.budget - allocated

a9 := chk("A9", "Budget allocation", "fail", "Request a monthly token allocation.", "GR-07") if {
	not m.tokens > 0
} else := chk("A9", "Budget allocation", "fail", sprintf("Requests %vM tokens a month; %s has %vM unallocated of %vM.", [m.tokens, bu.name, free, bu.budget]), "GR-07") if {
	m.tokens > free
} else := chk("A9", "Budget allocation", "pass", sprintf("%vM tokens a month from %vM unallocated.", [m.tokens, free]), "GR-07")

# A10 approval
a10 := chk("A10", "Approval", "pending", "Risk Office sign-off needed. No identity is issued until then.", "GR-09") if {
	tier == "high"
} else := chk("A10", "Approval", "pass", sprintf("Auto-approved at %s tier.", [tier]), "GR-09")

checks := [a1, a2, a3, a4, a5, a6, a7, a8, a9, a10]

outcome := "rejected" if {
	some c in checks
	c.status == "fail"
} else := "pending" if {
	tier == "high"
} else := "admitted"

decision := {"checks": checks, "outcome": outcome, "tier": tier, "risk_score": score}
