---
title: Admin Console: Users, Audit Log and Allowlists
doc_id: admin-console
audience: customer
effective: 2025-04-01
supersedes: none
---

# Admin Console: Users, Audit Log and Allowlists

This document explains how Northwind Cloud's Admin Console manages user lifecycle, audit logging, and IP allowlists. It applies to all workspaces on the Business and Enterprise plans, which include the Admin Console module.

## User Deactivation vs Deletion

Admins can remove a user's access in two ways, and the distinction matters for data retention and billing.

1. Deactivation: The user's login is disabled immediately, their active sessions are terminated within 5 minutes, and their seat license is released back to the pool. All content the user created, including documents, comments, and shared links, remains fully intact and accessible to other workspace members. A deactivated user can be reactivated at any time, and their history is restored exactly as it was.
2. Deletion: The user's account and personal profile data are permanently removed. Content ownership transfers to the workspace's designated default owner, or to a manager the admin selects at the time of deletion. Deleted accounts enter a 30 day grace period during which an admin can reverse the deletion; after 30 days, the account record is purged and cannot be recovered.

We recommend deactivation for employees on leave or role changes, and deletion only for confirmed offboarding.

## Audit Log Contents

The audit log records administrative and security relevant events across the workspace. Each entry includes:

1. Timestamp in UTC
2. Actor: the user or API token that performed the action
3. Action type, such as login, permission change, deactivation, deletion, or allowlist update
4. Target object affected, such as a user, workspace setting, or document
5. Source IP address and, where available, approximate geographic region

Audit log retention is 90 days on the Business plan and 400 days on the Enterprise plan. Logs can be exported as CSV from the Admin Console, or streamed continuously to a SIEM endpoint via the Audit Log Export API, available on the Enterprise plan. Exports include all fields above and are limited to 100,000 rows per CSV request; larger ranges should be pulled in multiple date-bounded exports.

## IP Allowlist Behavior

The IP allowlist restricts console and API access to approved network ranges. It is available on the Enterprise plan and is configured under Admin Console > Security > Network Access.

1. Once enabled, only requests originating from listed IP ranges are permitted for every account in the workspace, including owners and admins. There is no automatic exemption for administrative roles.
2. Changes to the allowlist take effect within 2 minutes of saving.
3. Because the allowlist applies uniformly, misconfiguration can lock out all admins simultaneously. To prevent permanent lockout, we strongly recommend adding at least two separate IP ranges before enabling enforcement, and testing access from each range prior to saving.

### Break Glass Procedure

If all admins are locked out, Northwind Cloud provides a break glass recovery path:

1. The workspace owner of record contacts Northwind Cloud Support and requests emergency access recovery.
2. Support verifies the requester's identity using the account's registered billing email and a secondary verification method, such as a phone call to the number on file.
3. Upon verification, Support temporarily disables the IP allowlist for that workspace for a window of 24 hours, allowing an admin to log in from any network and reconfigure the allowlist correctly.
4. If the allowlist is not corrected within the 24 hour window, enforcement automatically re-enables using the last saved configuration.

Break glass access is logged in the audit log as a Support initiated action and is visible to all admins once access is restored.

## Workspace Settings Inheritance

For accounts with multiple sub-workspaces, settings such as password policy, session timeout, and default sharing permissions are inherited from the parent workspace by default. Sub-workspace admins can override inherited settings individually, but the IP allowlist and audit log retention period are exceptions: these two settings are always controlled at the parent workspace level and cannot be overridden by sub-workspaces, to ensure consistent security posture across the account.

When a new sub-workspace is created, it snapshots the parent's settings at creation time. Later changes to the parent's settings do not automatically propagate to existing sub-workspaces; admins must apply updates manually to each sub-workspace, except for the allowlist and audit retention settings noted above, which update everywhere immediately.

## Summary of Plan Availability

| Feature | Business Plan | Enterprise Plan |
|---|---|---|
| User deactivation and deletion | Included | Included |
| Audit log retention | 90 days | 400 days |
| Audit log CSV export | Included | Included |
| Audit Log Export API (SIEM streaming) | Not included | Included |
| IP allowlist | Not included | Included |

Admins with questions about upgrading plan tiers or configuring these features should contact their Northwind Cloud account team or open a support ticket from within the Admin Console.
