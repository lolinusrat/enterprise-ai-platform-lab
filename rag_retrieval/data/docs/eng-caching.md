---
title: Caching guidelines
department: Engineering
classification: internal
groups: engineering
updated: 2025-08-30
---
Use the shared Redis cluster for application caches. Every key must have a TTL; the default is one hour. Prefix keys with the service name to avoid collisions.

Do not cache personal data for longer than 24 hours, and never cache authorization decisions across users.
