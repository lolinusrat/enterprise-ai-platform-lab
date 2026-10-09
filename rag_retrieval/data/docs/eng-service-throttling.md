---
title: Internal service throttling with Envoy
department: Engineering
classification: internal
groups: engineering
updated: 2026-04-27
---
Service-to-service calls inside the mesh are protected by Envoy local rate limits and circuit breakers. Each service declares its maximum concurrent requests in its mesh configuration.

When a dependency is overloaded, Envoy returns 503 with the header x-envoy-overloaded. Callers should back off and use cached data where possible rather than retrying in a tight loop.
