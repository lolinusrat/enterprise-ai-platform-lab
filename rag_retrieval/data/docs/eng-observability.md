---
title: Service observability standards
department: Engineering
classification: internal
groups: engineering
updated: 2026-02-14
---
Every service exports traces and metrics with OpenTelemetry and logs in JSON. Dashboards must show the four golden signals: latency, traffic, errors and saturation.

Tier-1 services define an availability SLO of 99.9% per 30 days with burn-rate alerts. Alerts must link to a runbook.
