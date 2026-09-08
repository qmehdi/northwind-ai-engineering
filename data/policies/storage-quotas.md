---
title: Storage Quotas and Backups
doc_id: storage-quotas
audience: customer
effective: 2025-02-01
supersedes: none
---

# Storage Quotas and Backups

This document explains how storage quotas work across Northwind Cloud plans, what happens when an account reaches its limit, and how backups and restores are handled. It applies to all customers on Starter, Pro, and Enterprise plans effective 2025-02-01.

## Plan Quotas

Each plan includes a fixed storage allowance. Quotas are expandable on request, subject to the process described below.

| Plan | Included Storage |
|------|-------------------|
| Starter | 50 GB |
| Pro | 500 GB |
| Enterprise | 5 TB |

Storage usage includes uploaded files, generated reports, and attached media, but does not include backup archives, which are counted separately.

## Usage Notifications

To help teams plan ahead, Northwind Cloud sends automated notifications as accounts approach their limit:

1. At 80 percent of quota, account administrators receive an email notice.
2. At 90 percent of quota, a warning banner appears in the dashboard for all workspace members.
3. At 100 percent of quota, the account enters a restricted state described in the next section.

## Behavior at 100 Percent Capacity

When an account reaches 100 percent of its storage quota, the following restrictions apply immediately:

1. New file uploads are refused until storage is freed or the quota is increased.
2. Scheduled backups are paused; no new backup snapshots are created while the account remains at capacity.
3. Existing data remains fully readable and accessible; customers can view, download, and export all stored files without interruption.

Once usage falls back below 100 percent, either through deletion of data or a quota increase, uploads and backups resume automatically without requiring a support ticket.

## Emergency Quota Increases

Enterprise customers who need additional storage on short notice can request an emergency quota increase through the support portal or by contacting their account manager. Northwind Cloud commits to processing emergency increases for Enterprise accounts within 4 hours of request submission. Starter and Pro customers can also request quota increases, but these are handled as standard plan upgrades rather than emergency requests, and turnaround depends on the upgrade path chosen.

## Backup Schedule and Retention

Northwind Cloud performs backups on the following schedule for all plans:

1. Backups run nightly, capturing a full snapshot of account data.
2. Each backup snapshot is kept for 35 days from the date it was created.
3. After 35 days, older snapshots are automatically purged to make room for new ones.

Backups are stored separately from primary storage and do not count against the plan quotas listed above. If an account is canceled, backup snapshots created before cancellation remain available for restore requests until their normal 35 day retention period expires, after which they are permanently deleted.

## Restore Requests and Turnaround

Customers can request a restore from any available backup snapshot by submitting a request through the support portal. Restore turnaround times vary by plan:

| Plan | Restore Turnaround |
|------|---------------------|
| Enterprise | 4 hours |
| Pro | 1 business day |
| Starter | 3 business days |

When submitting a restore request, customers should specify the account, the date of the desired snapshot, and whether a full or partial restore is needed. Partial restores, such as recovering a single deleted file or folder, are supported on all plans and generally complete within the same turnaround window listed above. Full account restores may take longer for very large Enterprise accounts approaching the 5 TB quota, and support will provide an updated estimate if this applies.

## Support and Escalation

For questions about current usage, quota upgrades, or backup status, customers can check the storage dashboard within their account or contact support directly. Enterprise customers with an assigned account manager should route emergency quota increase requests and urgent restore requests through that contact to ensure the fastest possible response within the committed 4 hour window. All other requests, including standard quota upgrades and non-urgent restores, should go through the standard support portal ticketing system, where response times follow the turnaround table above based on plan tier.
