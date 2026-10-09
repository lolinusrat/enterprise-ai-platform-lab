# Grant policy: may the authorization server issue a grant for this customer and purpose?
#
# Asked by the authorization server when the agent calls request_authorization, and asked again by
# access.rego on every record read, so an entitlement removed after a grant was issued takes effect at once.
#
# input: {user, agent, customer_id, purpose, reference}
#   user  - the signed-in employee, set by the runtime from the session (never by the model)
#   agent - the agent's workload identity
package custauthz.grant

import rego.v1

default allow := false

allow if count(deny) == 0

employee := data.secagent.employees[input.user]

agent := data.secagent.agents[input.agent]

purpose := data.secagent.purposes[input.purpose]

customer := data.secagent.customers[input.customer_id]

deny contains {"id": "G1", "msg": sprintf("unknown employee %q", [input.user])} if not employee

deny contains {"id": "G1", "msg": sprintf("agent %q is not registered", [input.agent])} if not agent

deny contains {"id": "G2", "msg": sprintf("unknown customer %q", [input.customer_id])} if not customer

deny contains {"id": "G3", "msg": sprintf("%q is not a recognised purpose", [input.purpose])} if not purpose

deny contains {"id": "G4", "msg": msg} if {
	not employee.role in purpose.roles
	msg := sprintf("role %s may not access customer data for %s", [employee.role, input.purpose])
}

deny contains {"id": "G5", "msg": msg} if {
	employee.role in purpose.roles
	customer
	not relationship
	msg := sprintf("no %s links %s to %s", [basis_label[purpose.basis], input.user, input.customer_id])
}

deny contains {"id": "G6", "msg": msg} if {
	customer.restricted
	not purpose.allows_restricted
	msg := sprintf("%s is a restricted record; only an open fraud case can unlock it", [input.customer_id])
}

basis_label := {
	"assignment": "book-of-business assignment",
	"ticket": "open support ticket assigned to the employee",
	"case": "open fraud case owned by the employee",
	"consent": "recorded marketing consent",
}

relationship if {
	purpose.basis == "assignment"
	input.customer_id in data.secagent.assignments[input.user]
}

relationship if {
	purpose.basis == "ticket"
	ticket := data.secagent.tickets[input.reference]
	ticket.customer == input.customer_id
	ticket.assignee == input.user
	ticket.status == "open"
}

relationship if {
	purpose.basis == "case"
	case := data.secagent.cases[input.reference]
	case.customer == input.customer_id
	case.investigator == input.user
	case.status == "open"
}

relationship if {
	purpose.basis == "consent"
	customer.marketing_consent
}

default fields := []

fields := purpose.fields if allow

default ttl_seconds := 0

ttl_seconds := purpose.ttl_seconds if allow

decision := {
	"allow": allow,
	"deny": sort(deny),
	"fields": fields,
	"ttl_seconds": ttl_seconds,
}

