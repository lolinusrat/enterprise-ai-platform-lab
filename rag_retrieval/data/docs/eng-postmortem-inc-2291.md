---
title: Postmortem INC-2291 - checkout latency
department: Engineering
classification: internal
groups: engineering
updated: 2026-08-21
---
On 14 August 2026 checkout p99 latency rose from 400 ms to 9 seconds for 47 minutes, and about 3% of checkouts failed.

Root cause: a configuration change lowered max_client_conn in pgbouncer from 2000 to 200 during a routine cleanup. Under evening peak load the connection pool was exhausted and requests queued waiting for a database connection.

The change was rolled back at 19:52 and latency recovered within two minutes. Follow-ups: alert on pgbouncer waiting clients, require review from the database team for pooler configuration, and add a load test for checkout before config changes ship.
