---
title: Public API rate limits
department: Engineering
classification: internal
groups: engineering
updated: 2026-02-27
---
Each API key may make 600 requests per minute, with bursts of up to 100 requests per second. Enterprise customers can request a higher limit through their account team.

When a client exceeds its limit the API responds with HTTP 429 Too Many Requests and a Retry-After header in seconds. Clients should wait for that period rather than retrying immediately. The X-RateLimit-Remaining header shows the requests left in the current window.
