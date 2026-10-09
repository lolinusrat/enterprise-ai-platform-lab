---
title: Production deploy runbook
department: Engineering
classification: internal
groups: engineering
updated: 2026-06-30
---
All services deploy to production through Argo CD from the main branch. A deploy requires a green CI pipeline and two approvals on the pull request, at least one from a code owner.

Deploy freeze: no production deploys after 14:00 on Fridays, on public holidays, or during the end-of-quarter freeze announced by Release Engineering. Emergency fixes during a freeze need approval from the incident commander.

To roll back, run `argocd app rollback <app> <revision>` or use "History and rollback" in the Argo CD UI, then post in #deploys. Roll back first and debug afterwards: if error rate or latency alerts fire within 30 minutes of a deploy, assume the deploy caused it.
