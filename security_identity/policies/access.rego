# Access policy: the tool gateway asks this for EVERY tool call the agent makes.
#
# The agent's instructions play no part here. The decision depends only on who the session belongs to,
# which agent is calling, which tool, and - for record reads - a grant whose signature OPA verifies itself
# against the authorization server's public key.
#
# input: {user, agent, session, tool, args}
#   user, agent, session - set by the runtime from the authenticated session, never by the model
#   tool, args           - exactly what the model asked for
package custauthz.access

import rego.v1

default allow := false

allow if count(deny) == 0

agent := data.secagent.agents[input.agent]

tool := data.secagent.tools[input.tool]

needs_grant if tool.requires_grant

grant := object.get(input.args, "grant", "")

requested := object.get(input.args, "customer_id", "")

# --- every tool ------------------------------------------------------------------------------

deny contains {"id": "T1", "msg": sprintf("agent %q is not registered", [input.agent])} if not agent

deny contains {"id": "T1", "msg": sprintf("tool %q is not in the agent's manifest", [input.tool])} if {
	agent
	not input.tool in agent.tools
}

deny contains {"id": "T2", "msg": sprintf("%q is not a known employee", [input.user])} if {
	not data.secagent.employees[input.user]
}

# --- tools that read customer data -----------------------------------------------------------

deny contains {"id": "R1", "msg": "no grant presented; call request_authorization first and pass the grant"} if {
	needs_grant
	grant == ""
}

deny contains {"id": "R2", "msg": "grant is not a token signed by the authorization server"} if {
	needs_grant
	grant != ""
	not signature_ok
}

signature_ok if io.jwt.verify_rs256(grant, data.secagent.jwks)

# Claims are read only from a token whose signature has been verified.
claims := io.jwt.decode(grant)[1] if signature_ok

deny contains {"id": "R3", "msg": "grant has expired"} if {
	needs_grant
	claims.exp * 1000000000 <= time.now_ns()
}

deny contains {"id": "R4", "msg": "grant was not issued for the tool gateway"} if {
	needs_grant
	signature_ok
	not valid_issuer_audience
}

valid_issuer_audience if {
	claims.iss == data.secagent.issuer
	claims.aud == data.secagent.audience
}

deny contains {"id": "R5", "msg": sprintf("grant belongs to %s, not %s", [claims.sub, input.user])} if {
	needs_grant
	claims.sub != input.user
}

deny contains {"id": "R6", "msg": "grant was issued to a different session"} if {
	needs_grant
	claims.sid != input.session
}

deny contains {"id": "R7", "msg": sprintf("grant was issued to agent %s", [claims.azp])} if {
	needs_grant
	claims.azp != input.agent
}

deny contains {"id": "R8", "msg": msg} if {
	needs_grant
	requested != ""
	claims.customer_id != requested
	msg := sprintf("grant covers %s, not %s", [claims.customer_id, requested])
}

deny contains {"id": "R8", "msg": "customer_id is required"} if {
	needs_grant
	not is_string(requested)
}

deny contains {"id": "R8", "msg": "customer_id is required"} if {
	needs_grant
	requested == ""
}

deny contains {"id": "R9", "msg": "grant has been revoked"} if {
	needs_grant
	claims.jti in data.secagent.revoked
}

# Re-check the entitlement now, for the customer actually requested: a grant is evidence of a past
# decision, not a licence that outlives the relationship it was based on.
deny contains {"id": "R10", "msg": sprintf("entitlement no longer holds: %s", [r.msg])} if {
	needs_grant
	some r in data.custauthz.grant.deny with input as {
		"user": input.user,
		"agent": input.agent,
		"customer_id": requested,
		"purpose": claims.purpose,
		"reference": object.get(claims, "ref", ""),
	}
}

# --- obligations ----------------------------------------------------------------------------

# Fields the gateway may return. Taken from the purpose in the verified grant, never from the request.
fields := data.secagent.purposes[claims.purpose].fields if needs_grant

fields := object.get(tool, "fields", []) if not needs_grant

default released_fields := []

released_fields := fields if allow

default grant_claims := null

grant_claims := claims

decision := {
	"allow": allow,
	"deny": sort(deny),
	"fields": released_fields,
	"grant_claims": grant_claims,
}
