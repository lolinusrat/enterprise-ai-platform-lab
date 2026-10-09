package controlplane.admission_test

import data.controlplane.admission

mortgage := {
	"bu": "retail", "name": "mortgage-prequal", "owner": "priya.shah@corp.example", "purpose": "x",
	"models": ["claude-sonnet-5-5", "gpt-oss-120b"], "cls": 3, "tools": ["core-banking.read", "crm.read"],
	"tier": "medium", "evalScore": 0.91, "hitl": false, "customerFacing": false, "tokens": 35,
}

status_of(d, id) := s if {
	some c in d.checks
	c.id == id
	s := c.status
}

test_mortgage_admitted_with_routing_warning if {
	d := admission.decision with input as {"manifest": mortgage} with data.cp as data.testdata.cp
	d.outcome == "admitted"
	d.tier == "medium"
	status_of(d, "A4") == "warn"
	not status_of(d, "A1") == "fail"
}

test_bu_cannot_widen_catalog if {
	m := object.union(mortgage, {"bu": "marketing", "name": "brand-voice", "models": ["claude-opus-5-5", "legacy-chat-v1"], "cls": 2, "tools": ["cms.publish", "web.search"]})
	d := admission.decision with input as {"manifest": m} with data.cp as data.testdata.cp
	d.outcome == "rejected"
	status_of(d, "A2") == "fail" # deprecated model
	status_of(d, "A3") == "fail" # Opus not entitled to Marketing
	status_of(d, "A4") == "fail" # Confidential above Marketing's Internal cap
}

test_declared_tier_cannot_be_lower_than_derived if {
	m := object.union(mortgage, {"name": "refunder", "tools": ["payments.refund"], "tier": "low"})
	d := admission.decision with input as {"manifest": m} with data.cp as data.testdata.cp
	status_of(d, "A6") == "fail"
	d.tier == "high"
	status_of(d, "A8") == "fail" # high tier writes without an approver
}

test_high_tier_waits_for_sign_off if {
	m := object.union(mortgage, {"name": "collections-agent", "tools": ["core-banking.read", "crm.write"], "tier": "high", "hitl": true, "customerFacing": true, "evalScore": 0.92})
	d := admission.decision with input as {"manifest": m} with data.cp as data.testdata.cp
	d.outcome == "pending"
}

test_existing_name_rejected if {
	m := object.union(mortgage, {"name": "branch-assist"})
	d := admission.decision with input as {"manifest": m} with data.cp as data.testdata.cp
	status_of(d, "A1") == "fail"
}

test_budget_over_allocation if {
	m := object.union(mortgage, {"tokens": 500})
	d := admission.decision with input as {"manifest": m} with data.cp as data.testdata.cp
	status_of(d, "A9") == "fail"
}
