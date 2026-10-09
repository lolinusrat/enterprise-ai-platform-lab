---
title: Load testing guidelines
department: Engineering
classification: internal
groups: engineering
updated: 2026-01-22
---
Use k6 for load tests and run them against the staging environment, never production. Tier-1 services must be load tested at twice the previous peak before major launches.

Record p50, p95 and p99 latency and the error rate for each test and attach the report to the launch checklist.
