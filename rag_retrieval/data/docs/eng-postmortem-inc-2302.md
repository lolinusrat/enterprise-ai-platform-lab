---
title: Postmortem INC-2302 - checkout errors
department: Engineering
classification: internal
groups: engineering
updated: 2026-09-10
---
On 3 September 2026 about 12% of checkout requests failed with TLS handshake errors for 22 minutes.

Root cause: the TLS certificate on the payments gateway load balancer expired because the renewal job had been failing silently since a credentials rotation. Follow-ups: alert on certificates expiring within 21 days and move the gateway to managed certificates.
