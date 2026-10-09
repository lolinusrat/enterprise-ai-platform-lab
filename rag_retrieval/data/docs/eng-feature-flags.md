---
title: Feature flag guidelines
department: Engineering
classification: internal
groups: engineering
updated: 2026-03-15
---
Use LaunchDarkly to release changes gradually. Every flag must have an owner and an expiry date.

Roll out in stages: internal users, then 5%, 25% and 100% of customers. If metrics degrade, switch the flag off; this acts as a kill switch and is faster than a rollback.

Remove the flag and the dead code path within 30 days of reaching 100%. Stale flags are reported weekly to each team.
