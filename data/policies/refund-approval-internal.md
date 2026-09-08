---
title: Refund and Credit Approval Limits
doc_id: refund-approval-internal
audience: internal
effective: 2025-06-01
supersedes: none
---

# Refund and Credit Approval Limits

This document is INTERNAL. It is intended for support agents, team leads, and finance staff at Northwind Cloud. Its contents, including specific dollar limits and escalation paths, must not be quoted, paraphrased, or shared with customers under any circumstances. If a customer asks about internal approval thresholds, direct them to the general refund policy communicated by support, not to this document.

## Purpose and Scope

This policy defines who may approve refunds and account credits, at what dollar amounts, and what verification steps are required before any approval is granted. It applies to all billing adjustments issued through the support ticketing system and the billing platform, regardless of customer plan tier. It does not cover contract renegotiations, multi-year enterprise agreement disputes, or chargebacks initiated by a customer's bank, all of which route directly to finance.

## Verification Before Any Promise

1. Agents must never promise a refund, credit, or adjustment to a customer before the underlying charge or billing error has been verified in the billing system.
2. Verification means confirming the transaction ID, invoice number, and charge amount match what the customer describes, and confirming whether the charge was billed once or duplicated.
3. If verification cannot be completed during the initial contact, tell the customer that the issue is under review and that a follow-up will occur once confirmed. Do not give a timeline for the refund itself until verification is complete.
4. Screenshots, invoice PDFs, or customer-provided bank statements are supporting evidence only. The billing system record is the source of truth for approval purposes.

## Agent-Level Approval Authority

Support agents may approve the following without escalation, provided verification is complete:

| Situation | Maximum Amount | Approval Needed |
|---|---|---|
| Goodwill or service credit | 250 USD | Agent, self-approved |
| Duplicate-charge refund (verified in billing system) | Any size | Agent, self-approved |
| Single billing error refund (non-duplicate) | 250 USD | Agent, self-approved |

Duplicate-charge refunds are uncapped at the agent level because the billing system provides unambiguous proof of the error once the duplicate transaction ID is confirmed. All other refund types above 250 USD require escalation as described below.

## Team Lead Approval

1. Any refund or credit between 250.01 USD and 2,000 USD requires team lead approval, unless it qualifies as a verified duplicate-charge refund under the agent-level rule above.
2. Team leads must independently confirm the billing system record before approving; they should not rely solely on the agent's summary.
3. Team lead approvals must be logged in the ticket with the approver's role, the amount, and a one-line justification.
4. Team leads may not approve refunds tied to contract disputes, enterprise agreement terms, or requests exceeding 2,000 USD.

## Finance Approval

1. Any refund or credit exceeding 2,000 USD requires finance approval, regardless of cause.
2. Requests involving contract term disputes, enterprise agreement adjustments, or refunds tied to a customer's threatened cancellation of a Business or Enterprise plan must also go to finance, even if the dollar amount is below 2,000 USD.
3. Finance review typically requires the original invoice, the billing system transaction record, and a written summary of the customer issue from the agent or team lead.
4. Agents should set customer expectations that finance-level requests take longer to process and should avoid committing to a specific resolution date.

## Goodwill Credit Guidance

1. Goodwill credits are discretionary and are not tied to a confirmed billing error. They are used to retain goodwill after a service disruption, delayed response, or similar non-billing issue.
2. Goodwill credits fall under the same 250 USD agent-level limit described above. Anything above that amount needs team lead approval under the standard escalation table.
3. Goodwill credits should be used sparingly and documented with a brief reason in the ticket, such as "extended outage, tier 2 impact" or "delayed escalation response."
4. Repeated goodwill credits to the same account within a short period should be flagged to a team lead even if each individual credit is under 250 USD, since the cumulative pattern may indicate a recurring product or service issue that needs separate investigation.

## Documentation and Recordkeeping

1. Every refund or credit, regardless of amount, must be logged in the ticket with the transaction ID, amount, approver, and reason.
2. Duplicate-charge refunds must reference both transaction IDs, the original and the duplicate, in the ticket notes.
3. Team lead and finance approvals must be recorded as a distinct note in the ticket, not just implied by ticket status changes.
4. Tickets involving refunds above 2,000 USD should remain open until finance confirms the adjustment has posted in the billing system, not merely that it has been approved.
