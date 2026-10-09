---
title: Kafka consumer guidelines
department: Engineering
classification: internal
groups: engineering
updated: 2026-02-03
---
Each consuming service uses its own consumer group named after the service. Commit offsets only after a message has been fully processed, and make handlers idempotent because messages can be delivered more than once.

Alert when consumer lag stays above five minutes. Send messages that repeatedly fail to a dead-letter topic instead of blocking the partition.
