---
title: Postmortem INC-2287 - search latency
department: Engineering
classification: internal
groups: engineering
updated: 2026-08-02
---
On 29 July 2026 product search p99 latency rose to 6 seconds for 35 minutes during the European morning peak.

Root cause: an automatic Elasticsearch shard rebalance started after a node was replaced, and the rebalance saturated disk I/O on the remaining nodes. Follow-ups: restrict rebalancing to off-peak hours and alert on relocating shards.
