# On-behalf-of scoping for the identity broker's token exchange.
# Query: data.controlplane.identity.exchange
# input: {"agent": "retail/branch-assist", "groups": ["all-staff", ...]}
package controlplane.identity

agent := data.cp.agents[input.agent]

# agent's declared tools ∩ tools the user is entitled to through their IdP groups
scopes := [t | some t in agent.tools; data.cp.tools[t].ent in input.groups]

dropped := [t | some t in agent.tools; not data.cp.tools[t].ent in input.groups]

active if agent.status == "active"

exchange := {"allowed": true, "scopes": scopes, "dropped": dropped} if {
	active
} else := {"allowed": false, "scopes": [], "dropped": []}
