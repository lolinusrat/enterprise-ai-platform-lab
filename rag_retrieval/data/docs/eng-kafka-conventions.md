---
title: Event streaming - Kafka topic conventions
department: Engineering
classification: internal
groups: engineering
updated: 2026-01-30
---
Topic names follow domain.entity.event.version, for example payments.charge.captured.v1. Create topics through the platform Terraform module, not by hand.

The default retention is 7 days. Topics that feed the data warehouse keep 30 days. Schemas are registered in the schema registry in Avro with BACKWARD compatibility, so consumers can always read new messages with an older schema.
