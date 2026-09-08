---
title: Data Retention and Deletion
doc_id: data-retention
audience: customer
effective: 2025-01-10
supersedes: none
---

# Data Retention and Deletion

This policy explains how Northwind Cloud retains, configures, and deletes customer data across our products. It applies to all customers on the Starter, Pro, and Enterprise plans unless a separate written agreement states otherwise.

## 1. Default Retention Periods

Northwind Cloud applies default retention periods by data category. Customers on Pro and Enterprise plans can adjust storage retention within the allowed range through account settings or by contacting support.

| Data category | Default retention | Configurable range | Notes |
|---|---|---|---|
| Primary storage (files, records) | 365 days | 30 to 2555 days | Adjustable per workspace |
| Audit logs, Enterprise plan | 400 days | Fixed | Not user configurable |
| Audit logs, Pro plan | 90 days | Fixed | Not user configurable |
| Audit logs, Starter plan | 30 days | Fixed | Not user configurable |
| Pipeline run logs | 30 days | Fixed | Applies to all plans |

Storage retention changes take effect within 24 hours and apply to newly ingested data going forward. Reducing the retention window does not immediately delete existing data past the new threshold; that data is removed during the next scheduled cleanup cycle, which runs daily.

## 2. Account Closure and Deletion

When an account is closed, whether by customer request or by non-renewal, Northwind Cloud follows this process:

1. All primary storage, audit logs, and pipeline run logs associated with the account are scheduled for deletion.
2. Deletion of all account data completes within 30 days of the closure date.
3. During this 30 day window, the account is placed in a read-only state and data cannot be modified, only exported.
4. After the 30 day window, data is not recoverable from production systems.

Customers who close their account voluntarily receive a confirmation notice with the exact deletion date calculated from the closure date.

## 3. Backup Retention

Independent of primary storage settings, Northwind Cloud maintains encrypted backups for disaster recovery purposes. Backups are retained for 35 days from the date of creation, after which they are automatically overwritten. Backups are not accessible through the customer dashboard and are used only for system restoration in the event of data loss or corruption. Because backups persist for up to 35 days, residual copies of deleted account data may exist in backup systems for that period even after production deletion is complete.

## 4. Legal Hold

In cases where Northwind Cloud is required to preserve data due to litigation, regulatory investigation, or a valid legal request, affected data will be placed under legal hold. Data under legal hold is:

1. Excluded from standard deletion schedules, including account closure deletion and backup rotation.
2. Retained until the hold is formally released by legal counsel or the requesting authority.
3. Restricted to access by authorized personnel only, logged separately from standard audit trails.

Customers subject to a legal hold will be notified where legally permitted, and normal retention settings resume automatically once the hold is lifted.

## 5. Exporting Data Before Deletion

Customers are responsible for exporting any data they wish to retain before scheduled deletion occurs. Northwind Cloud provides the following export options:

1. Self-service export through the dashboard, available at any time while the account is active or during the 30 day read-only period following closure.
2. Bulk export via API for accounts on the Pro and Enterprise plans, supporting scheduled or on-demand extraction.
3. A manual export request submitted to support for Starter plan customers who need assistance formatting or transferring data.

We recommend initiating exports at least 5 business days before any planned account closure to allow time for validation and troubleshooting, since exports cannot be performed once the 30 day deletion window has elapsed.

## 6. GDPR Erasure Requests

Customers and their end users located in jurisdictions covered by the General Data Protection Regulation may submit a request for erasure of personal data. Northwind Cloud handles these requests as follows:

1. Requests must be submitted through the designated privacy contact channel identified in the customer's account settings or agreement.
2. Northwind Cloud verifies the identity of the requester before processing.
3. Verified erasure requests are completed within 30 days of receipt, consistent with regulatory requirements.
4. Data subject to an active legal hold is excluded from erasure until the hold is released, and the requester is informed of this exception.
5. Confirmation of completed erasure is sent to the requester once the process is finished.

Erasure under this process applies to primary storage, audit logs, and pipeline run logs. Residual copies in backup systems are removed through the standard 35 day backup rotation described in Section 3.

## 7. Contact and Questions

Customers with questions about retention configuration, export procedures, legal hold status, or erasure requests should contact Northwind Cloud support through the account dashboard. Enterprise customers may also reach out through their assigned account contact for expedited handling of retention and deletion inquiries.
