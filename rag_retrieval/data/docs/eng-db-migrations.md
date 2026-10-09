---
title: Database migration standards
department: Engineering
classification: internal
groups: engineering
updated: 2025-12-09
---
Schema changes follow the expand and contract pattern: add the new column or table, deploy code that writes to both, backfill, switch reads, then remove the old structure in a later release.

Migrations that lock a table are not allowed on tables with more than 10 million rows during business hours. Use gh-ost for online schema changes on large MySQL tables, and run it from the migration runner, not from a laptop.

Every migration must be reversible or have a written rollback plan in the pull request.
