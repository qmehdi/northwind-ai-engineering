---
title: Multi-Factor Authentication
doc_id: mfa-policy
audience: customer
effective: 2024-11-01
supersedes: none
---

# Multi-Factor Authentication

This document describes how multi-factor authentication (MFA) works across Northwind Cloud workspaces, how administrators can enforce it, and how account recovery is handled when a user loses access to their authentication device. This policy is effective 2024-11-01.

## Supported MFA Methods

Northwind Cloud supports the following methods for verifying a second factor at login:

1. Authenticator app (TOTP): Users generate a time-based one-time code from an app such as a standard TOTP-compatible authenticator. This is the recommended default method for all users.
2. Security key (FIDO2/WebAuthn): Users register a physical hardware key or platform authenticator. This is the recommended method for administrator accounts and any user with elevated permissions.
3. SMS text message: Available as a fallback method only. SMS is discouraged because it is vulnerable to number porting and interception, and Northwind Cloud may deprecate it as a standalone factor in a future release. Workspaces that require the highest assurance level should disable SMS entirely in workspace security settings.

Users may register more than one method so that a backup path exists if a primary device is unavailable.

## Admin Enforcement Per Workspace

Workspace administrators control MFA requirements at the workspace level through the Security settings panel:

1. Optional: Users may enable MFA voluntarily but are not blocked from signing in without it.
2. Required for admins: All users holding an admin or owner role must complete MFA setup before accessing workspace settings.
3. Required for all users: Every member of the workspace must enroll in MFA within a grace period configured by the admin before restricted access takes effect.

Admins can also restrict which methods are permitted (for example, disabling SMS) and can view a per-user enrollment status report to confirm compliance across the workspace.

## Recovery Codes

When a user first enrolls in MFA, Northwind Cloud generates 10 single-use recovery codes. These codes:

1. Should be stored in a password manager or printed and kept in a secure physical location.
2. Can be used one at a time to sign in if the primary MFA device is unavailable.
3. Are automatically regenerated as a full new set of 10 whenever the user consumes a code or manually requests a reset, invalidating any unused codes from the prior set.

Users are strongly encouraged to store recovery codes before they need them, since generating new codes typically requires an already-authenticated session.

## Lost Device Recovery Through Support

If a user loses their MFA device and has no working recovery codes, Northwind Cloud support can assist with regaining access. The process is:

1. The user contacts support and requests an MFA reset.
2. Support performs identity verification, which includes confirming the account email address, verifying at least two account details on file (such as workspace name, billing contact, or last login location), and, where available, matching the request against previously registered contact information.
3. Once identity is confirmed, support disables the existing MFA method on the account and prompts the user to enroll a new method at next login.
4. If the account holds an admin or owner role, a mandatory 24 hour cooling period is applied between identity verification and the actual MFA reset taking effect. This cooling period exists to prevent social-engineering attacks against privileged accounts and cannot be waived by support staff.

During the cooling period, support will notify the workspace's other admins by email where more than one admin exists, so that a compromised request can be flagged and halted before the reset completes.

## Enterprise Plan Requirement Effective 2025-01-01

Starting 2025-01-01, MFA becomes mandatory for all admin accounts on the Enterprise plan. Key points:

1. This requirement applies to every user holding an admin or owner role within an Enterprise workspace, regardless of the workspace's current MFA setting.
2. Admin accounts that have not enrolled in MFA by 2025-01-01 will be prompted to complete setup at their next login and will be unable to access workspace settings until enrollment is finished.
3. Non-admin members of Enterprise workspaces are not affected by this mandatory requirement, although admins may still choose to enable the Required for all users enforcement level described above.
4. Enterprise workspace admins are encouraged to review current enrollment status well ahead of 2025-01-01 using the workspace Security settings report, to avoid disruption when the requirement takes effect.

## Recommendations for Workspace Admins

To reduce the risk of lockouts and support escalations, Northwind Cloud recommends that workspace admins:

1. Set the workspace enforcement level to at least Required for admins, even on plans where this is not yet mandatory.
2. Disable SMS as an available method for admin and owner roles.
3. Remind all users to generate and securely store their 10 recovery codes immediately after enrollment.
4. Maintain at least two active admins per workspace so that the notification step during the 24 hour cooling period has a recipient other than the account under recovery.

Questions about MFA enforcement, recovery code regeneration, or the 2025-01-01 Enterprise requirement should be directed to Northwind Cloud support.
