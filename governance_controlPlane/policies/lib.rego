package controlplane.lib

classes := ["Public", "Internal", "Confidential", "Restricted"]

step(comp, check, status, detail, rule) := {"comp": comp, "check": check, "status": status, "detail": detail, "rule": rule}

# Keep steps up to and including the first deny: later checks are never reached.
upto_deny(raw) := array.slice(raw, 0, min(ix) + 1) if {
	ix := [i | some i, s in raw; s.status == "deny"]
	count(ix) > 0
} else := raw

decision(steps) := "deny" if {
	some s in steps
	s.status == "deny"
} else := "hold" if {
	some s in steps
	s.status == "hold"
} else := "obligations" if {
	some s in steps
	s.status == "warn"
} else := "allow"

join(xs) := concat(", ", xs)
