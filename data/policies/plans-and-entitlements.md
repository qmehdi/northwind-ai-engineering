---
title: Plans, Seats and Entitlements
doc_id: plans-and-entitlements
audience: customer
effective: 2025-06-01
supersedes: none
---

# Plans, Seats and Entitlements

This document describes what is included in each Northwind Cloud subscription plan, how seats are counted and billed, and the rules that apply when you change seat counts, exceed usage limits, use a trial, or downgrade your plan. It applies to all customer accounts effective 2025-06-01.

## Plan Tiers and Features

Northwind Cloud offers three subscription tiers. Each tier includes all features of the tier below it, plus the additions listed.

| Plan | Core Features |
|------|----------------|
| Starter | Dashboards, basic API access |
| Pro | Everything in Starter, plus full API access, single sign-on (SSO), third-party integrations |
| Enterprise | Everything in Pro, plus SCIM provisioning, audit log, data residency options, priority support |

1. Starter is intended for small teams that need reporting dashboards and limited programmatic access through the basic API.
2. Pro is intended for teams that require unrestricted API usage, centralized authentication through SSO, and connections to third-party tools.
3. Enterprise is intended for organizations that require automated user lifecycle management through SCIM, a full audit trail of account activity, control over the geographic region where data is stored, and priority support handling.
4. Enterprise data residency options currently include the United States and the European Union. Customers must select a region at contract signing; region changes after signing require a support request and may involve a migration window.
5. Enterprise priority support includes a 4 hour first response service level for tickets marked urgent, compared to standard next business day response on Starter and Pro plans.

## Seat Counting and Billing

1. A seat is counted for each active user in the billing period, regardless of how many days that user was active during the period.
2. An active user is any account holder who logs in, is provisioned through SSO or SCIM, or otherwise accesses the platform at least once during the billing period.
3. Deactivated or suspended accounts that had no activity in the billing period are not counted as active seats for that period.
4. Seat counts are calculated once per billing period for invoicing purposes, but the underlying activity is tracked continuously so that mid-cycle changes can be reconciled accurately.
5. Invoices show the total active seat count for the period and the corresponding per-seat charge based on the subscribed plan.

## Mid-Cycle Seat Changes

1. Seats added during a billing period are prorated daily from the date the new user first becomes active.
2. Seats removed during a billing period are prorated daily up to the date the user is deactivated; no credit is issued for partial-day usage on the deactivation date itself.
3. Proration is calculated using the number of days remaining in the current billing period divided by the total number of days in that period, applied to the per-seat rate for the plan.
4. Seat changes are reflected on the next invoice following the change; customers do not receive a separate invoice for each individual seat change.
5. Bulk seat changes made through SCIM on Enterprise plans follow the same daily proration rule as manual changes made in the admin console.

## Overage Handling

1. Overage occurs when the number of active seats in a billing period exceeds the number of seats included in the customer's current subscription commitment.
2. Overage seats are billed at the standard per-seat rate for the customer's plan tier and are included on the invoice for the period in which the overage occurred.
3. Customers are notified by email when active seat usage exceeds 90 percent of their committed seat count, and again if usage exceeds the committed count.
4. Accounts have a grace period of 5 business days after an overage notification to adjust seat counts or contact billing before the overage is finalized on the invoice.
5. Repeated overages across three or more consecutive billing periods may prompt Northwind Cloud to recommend a plan or commitment adjustment during account review.

## Trial Rules

1. New customers may request a trial of the Pro or Enterprise plan for a period of 14 days.
2. Trials include full access to the features of the selected plan tier, subject to standard fair use limits on API calls and data storage.
3. No seat charges are billed during the trial period, and no credit card is required to begin a Starter trial.
4. At the end of the 14 day trial period, the account automatically reverts to the Starter plan unless the customer selects a paid plan before the trial ends.
5. Data created during a trial is retained for 30 days after the trial ends to allow the customer to upgrade without data loss; after that window, trial data on downgraded accounts may be permanently deleted.
6. Only one trial per plan tier is permitted per customer account.

## Downgrade Restrictions

1. Downgrades from Enterprise to Pro or Starter, or from Pro to Starter, take effect at the start of the next billing period, not immediately upon request.
2. Features exclusive to the higher tier, including SCIM, audit log, data residency selection, and priority support for Enterprise, or SSO, full API access, and integrations for Pro, are disabled at the effective date of the downgrade.
3. Customers must remove or reconfigure any integrations, SSO connections, or SCIM provisioning rules that depend on features not available in the target plan before the downgrade takes effect; unresolved dependencies may cause user access issues.
4. Audit log and data residency records accumulated on Enterprise are retained for 30 days after downgrade for export purposes, after which they may be deleted.
5. Seat counts in excess of the target plan's included seats at the time of downgrade are billed as overage under the new plan's per-seat rate until seat counts are reduced.
6. Downgrade requests must be submitted through the account admin console or by contacting support; downgrades cannot be reversed retroactively within the same billing period.
