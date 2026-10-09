---
title: Billing API error codes
department: Engineering
classification: internal
groups: engineering
updated: 2026-03-19
---
The Billing API uses the same error envelope as the other public APIs: a code, a human-readable message and a retryable flag.

BILL-4031: invoice already finalized; finalized invoices cannot be edited, issue a credit note instead. BILL-4032: currency mismatch between the customer and the price. BILL-4040: subscription not found.

BILL-5002: timeout from the tax calculation service. Retryable with the same idempotency key, at most 3 attempts with exponential backoff.
