---
title: API versioning and deprecation
department: Engineering
classification: internal
groups: engineering
updated: 2025-11-14
---
Public APIs are versioned in the URL path (/v1, /v2). Additive changes such as new optional fields do not need a new version; removing or renaming fields does.

Deprecated versions are supported for 12 months after the replacement is generally available. Responses from deprecated versions carry a Deprecation header and a Sunset date.
