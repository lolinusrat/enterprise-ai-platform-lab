---
title: Secrets management
department: Security
classification: internal
groups: engineering, security
updated: 2026-04-02
---
Store credentials, API keys, tokens and certificates in HashiCorp Vault. Services read secrets at startup through the Vault agent sidecar. Never put secrets in source code, environment files committed to git, tickets or chat.

The git-secrets pre-commit hook blocks common key formats. If a secret leaks, rotate it immediately, then open a security ticket; deleting the commit is not enough.
