package controlplane.runtime_test

import data.controlplane.identity
import data.controlplane.runtime

user(sub, bu) := {"sub": sub, "bu": bu, "role": "x", "groups": []}

test_suspended_agent_denied if {
	r := runtime.invoke with input as {"agent": "marketing/seo-scout", "user": user("k.berg", "marketing")} with data.cp as data.testdata.cp
	r.decision == "deny"
	count(r.steps) == 1
}

test_cross_bu_denied if {
	r := runtime.invoke with input as {"agent": "people/hr-helpdesk", "user": user("j.okafor", "retail")} with data.cp as data.testdata.cp
	r.decision == "deny"
	r.steps[1].rule == "GR-10"
}

test_external_model_redacts_pii if {
	r := runtime.model with input as {"agent": "retail/branch-assist", "model": "claude-sonnet-5-5", "cls": 2, "declared_cls": 2, "pii_entities": ["email", "phone"]} with data.cp as data.testdata.cp
	r.decision == "obligations"
	r.redact == true
	r.route == "claude-sonnet-5-5"
}

test_restricted_prompt_rerouted_in_boundary if {
	r := runtime.model with input as {"agent": "retail/dispute-resolver", "model": "claude-sonnet-5-5", "cls": 3, "declared_cls": 3, "pii_entities": ["person name"]} with data.cp as data.testdata.cp
	r.route == "gpt-oss-120b"
	r.redact == false
}

test_class_above_agent_ceiling_denied if {
	r := runtime.model with input as {"agent": "marketing/campaign-writer", "model": "claude-sonnet-5-5", "cls": 2, "declared_cls": 2, "pii_entities": []} with data.cp as data.testdata.cp
	r.decision == "deny"
	r.steps[1].rule == "GR-03"
}

test_model_not_in_manifest_denied if {
	r := runtime.model with input as {"agent": "retail/branch-assist", "model": "gpt-oss-120b", "cls": 1, "declared_cls": 1, "pii_entities": []} with data.cp as data.testdata.cp
	r.decision == "deny"
	count(r.steps) == 1
}

test_high_risk_write_held if {
	r := runtime.tool with input as {"agent": "retail/dispute-resolver", "tool": "payments.refund", "user": "m.rossi", "scopes": ["payments.refund"]} with data.cp as data.testdata.cp
	r.decision == "hold"
}

test_tool_outside_obo_scope_denied if {
	r := runtime.tool with input as {"agent": "wealth/advisor-copilot", "tool": "crm.write", "user": "s.patel", "scopes": ["crm.read"]} with data.cp as data.testdata.cp
	r.decision == "deny"
	r.steps[1].rule == "GR-05"
}

test_obo_scope_is_agent_and_user if {
	x := identity.exchange with input as {"agent": "wealth/advisor-copilot", "groups": ["all-staff", "crm-reader", "wealth-advisor"]} with data.cp as data.testdata.cp
	x.scopes == ["portfolio.read", "crm.read", "research.search"]
	x.dropped == ["crm.write"]
}

test_no_obo_for_suspended_agent if {
	x := identity.exchange with input as {"agent": "marketing/seo-scout", "groups": ["all-staff"]} with data.cp as data.testdata.cp
	x.allowed == false
}
