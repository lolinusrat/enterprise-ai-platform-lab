# Unit tests for grant.rego and access.rego. Grants are signed here with the test-only key in testdata.
#   docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa:1.10.1 test /policies -v
package custauthz.policy_test

import rego.v1

td := data.testdata.secagent

now_s := floor(time.now_ns() / 1000000000)

base_claims := {
	"iss": td.issuer, "aud": td.audience, "sub": "u-alice", "sid": "s1", "azp": "cust-assist",
	"customer_id": "C-1001", "purpose": "account_servicing", "jti": "j1", "iat": now_s, "exp": now_s + 300,
}

sign(claims) := io.jwt.encode_sign({"alg": "RS256", "typ": "JWT", "kid": "authz-1"}, claims, json.unmarshal(td.test_private_jwk))

read(token, customer, user, session) := d if {
	d := data.custauthz.access.decision with data.secagent as td with input as {
		"user": user, "agent": "cust-assist", "session": session, "tool": "get_customer_record",
		"args": {"customer_id": customer, "grant": token},
	}
}

ask_grant(user, customer, purpose, ref) := d if {
	d := data.custauthz.grant.decision with data.secagent as td with input as {
		"user": user, "agent": "cust-assist", "customer_id": customer, "purpose": purpose, "reference": ref,
	}
}

ids(decision) := {r.id | some r in decision.deny}

# --- grant policy ---------------------------------------------------------------------------

test_grant_assigned_rm if {
	d := ask_grant("u-alice", "C-1001", "account_servicing", "")
	d.allow
	"balance" in d.fields
	d.ttl_seconds == 300
}

test_grant_unassigned_rm_denied if {
	d := ask_grant("u-alice", "C-1004", "account_servicing", "")
	not d.allow
	ids(d) == {"G5"}
	d.fields == []
}

test_grant_wrong_role if ids(ask_grant("u-bob", "C-1001", "account_servicing", "")) == {"G4"}

test_grant_unknown_purpose if ids(ask_grant("u-alice", "C-1001", "curiosity", "")) == {"G3"}

test_grant_unknown_customer if "G2" in ids(ask_grant("u-alice", "C-9999", "account_servicing", ""))

test_grant_unknown_user if "G1" in ids(ask_grant("u-mallory", "C-1001", "account_servicing", ""))

test_grant_support_needs_open_ticket if {
	ask_grant("u-bob", "C-1002", "support_ticket", "T-501").allow
	ids(ask_grant("u-bob", "C-1001", "support_ticket", "T-502")) == {"G5"} # closed
	ids(ask_grant("u-bob", "C-1001", "support_ticket", "T-501")) == {"G5"} # other customer
}

test_grant_support_fields_minimised if {
	d := ask_grant("u-bob", "C-1002", "support_ticket", "T-501")
	not "balance" in d.fields
	not "notes" in d.fields
}

test_grant_restricted_needs_fraud_case if {
	d := ask_grant("u-carol", "C-1003", "fraud_investigation", "FC-77")
	d.allow
	"ssn" in d.fields
	ids(ask_grant("u-carol", "C-1003", "fraud_investigation", "FC-00")) == {"G5"}
}

test_grant_marketing_needs_consent if {
	ids(ask_grant("u-dave", "C-1001", "marketing_outreach", "")) == {"G5"}
	ask_grant("u-dave", "C-1005", "marketing_outreach", "").allow
}

# --- access policy --------------------------------------------------------------------------

test_read_with_valid_grant if {
	d := read(sign(base_claims), "C-1001", "u-alice", "s1")
	d.allow
	"balance" in d.fields
	not "ssn" in d.fields
	d.grant_claims.customer_id == "C-1001"
}

test_read_without_grant if ids(read("", "C-1001", "u-alice", "s1")) == {"R1"}

test_read_with_made_up_grant if {
	d := read("ADMIN-OVERRIDE", "C-1001", "u-alice", "s1")
	ids(d) == {"R2"}
	d.grant_claims == null
	d.fields == []
}

test_read_with_tampered_grant if {
	parts := split(sign(base_claims), ".")
	forged_body := base64url.encode_no_pad(json.marshal(object.union(base_claims, {"customer_id": "C-1004"})))
	ids(read(concat(".", [parts[0], forged_body, parts[2]]), "C-1004", "u-alice", "s1")) == {"R2"}
}

test_read_with_grant_from_other_key if {
	other := io.jwt.encode_sign({"alg": "HS256"}, base_claims, {"kty": "oct", "k": base64url.encode_no_pad("attacker-secret")})
	ids(read(other, "C-1001", "u-alice", "s1")) == {"R2"}
}

test_read_expired if {
	"R3" in ids(read(sign(object.union(base_claims, {"exp": now_s - 1})), "C-1001", "u-alice", "s1"))
}

test_read_wrong_audience if {
	"R4" in ids(read(sign(object.union(base_claims, {"aud": "payments"})), "C-1001", "u-alice", "s1"))
}

test_read_other_users_grant if {
	d := read(sign(base_claims), "C-1001", "u-bob", "s2")
	{"R5", "R6"} & ids(d) == {"R5", "R6"}
	"R10" in ids(d) # Bob has no relationship to C-1001 either
}

test_read_other_session if ids(read(sign(base_claims), "C-1001", "u-alice", "s9")) == {"R6"}

test_read_other_agent if "R7" in ids(read(sign(object.union(base_claims, {"azp": "hr-bot"})), "C-1001", "u-alice", "s1"))

test_read_other_customer_with_grant if {
	d := read(sign(object.union(base_claims, {"customer_id": "C-1002"})), "C-1001", "u-alice", "s1")
	ids(d) == {"R8"}
}

test_read_injection_target if {
	# A grant for C-1002 used to read the restricted C-1003: wrong customer, and no entitlement to it.
	d := read(sign(object.union(base_claims, {"customer_id": "C-1002"})), "C-1003", "u-alice", "s1")
	ids(d) == {"R8", "R10"}
}

test_read_without_customer_id if {
	d := data.custauthz.access.decision with data.secagent as td with input as {
		"user": "u-alice", "agent": "cust-assist", "session": "s1", "tool": "get_customer_record",
		"args": {"grant": sign(base_claims)},
	}
	not d.allow
	"R8" in ids(d)
}

test_read_revoked if "R9" in ids(read(sign(object.union(base_claims, {"jti": "revoked-jti"})), "C-1001", "u-alice", "s1"))

test_read_entitlement_removed_after_grant if {
	# Alice is moved off C-1001 after her grant was issued; the still-valid grant no longer works.
	moved := object.union(td, {"assignments": {"u-alice": ["C-1002"]}})
	d := data.custauthz.access.decision with data.secagent as moved with input as {
		"user": "u-alice", "agent": "cust-assist", "session": "s1", "tool": "get_customer_record",
		"args": {"customer_id": "C-1001", "grant": sign(base_claims)},
	}
	ids(d) == {"R10"}
}

test_fields_come_from_grant_purpose if {
	claims := object.union(base_claims, {"sub": "u-bob", "purpose": "support_ticket", "ref": "T-501", "customer_id": "C-1002"})
	d := read(sign(claims), "C-1002", "u-bob", "s1")
	d.allow
	d.fields == td.purposes.support_ticket.fields
}

test_tool_not_in_manifest if {
	d := data.custauthz.access.decision with data.secagent as td with input as {
		"user": "u-alice", "agent": "cust-assist", "session": "s1", "tool": "export_all_customers", "args": {},
	}
	ids(d) == {"T1"}
}

test_find_customer_needs_no_grant if {
	d := data.custauthz.access.decision with data.secagent as td with input as {
		"user": "u-alice", "agent": "cust-assist", "session": "s1", "tool": "find_customer", "args": {"name": "Tom"},
	}
	d.allow
	d.fields == ["customer_id", "name"]
}

test_unregistered_agent if {
	d := data.custauthz.access.decision with data.secagent as td with input as {
		"user": "u-alice", "agent": "shadow-bot", "session": "s1", "tool": "find_customer", "args": {},
	}
	ids(d) == {"T1"}
}
