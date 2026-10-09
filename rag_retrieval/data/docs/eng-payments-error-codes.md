---
title: Payments API error codes
department: Engineering
classification: internal
groups: engineering
updated: 2026-04-11
---
The Payments API returns structured errors with a code, a message and a retryable flag.

PAY-4031: card declined by the issuer. Do not retry; ask the customer to use another payment method. PAY-4032: insufficient funds; do not retry automatically. PAY-4090: duplicate request, an identical idempotency key was already processed; return the original result.

PAY-5002: upstream timeout from the acquirer. This is retryable. Retry with the same idempotency key, using exponential backoff starting at 2 seconds, for at most 3 attempts. PAY-5031: acquirer unavailable; fail over to the secondary acquirer.
