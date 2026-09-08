---
title: Security Incident Response
doc_id: security-incident-response
audience: internal
effective: 2025-04-15
supersedes: none
---

# Security Incident Response

This document is INTERNAL. It is a runbook for Northwind Cloud staff only. Its contents must not be quoted, paraphrased, or forwarded to customers under any circumstances. Support and success teams must rely only on approved external messaging described below.

## Severity Levels

All suspected security incidents are triaged into one of four severity levels within 15 minutes of detection. Severity may be escalated but never downgraded without security lead sign-off.

| Level | Definition | Examples |
|---|---|---|
| SEV-1 | Confirmed or suspected cross-tenant data exposure, active exploitation, or credential compromise affecting production | One customer able to view another customer's records |
| SEV-2 | Contained vulnerability with limited blast radius, no confirmed data access | Misconfigured permission caught before exploitation |
| SEV-3 | Internal system anomaly with security relevance but no customer data risk | Suspicious login pattern on internal tooling |
| SEV-4 | Low-risk finding requiring tracking, not urgent response | Informational scan result |

## Who Is Paged

1. SEV-1: security lead, on-call engineering lead, VP of Engineering, and support lead are paged immediately via the incident hotline. Legal counsel is paged within 30 minutes.
2. SEV-2: security lead and on-call engineering lead are paged; support lead is notified but not paged.
3. SEV-3: security lead is notified during business hours; paging is not required outside business hours.
4. SEV-4: logged in the incident tracker for weekly review, no paging.
5. Any employee who discovers a suspected SEV-1 or SEV-2 condition must page the security lead directly, regardless of role, rather than waiting for a manager.

## Containment Steps for Cross-Tenant Data Exposure

1. Immediately revoke or restrict the access path (API key, session, permission grant) causing the exposure. Do not wait for root cause confirmation.
2. Isolate affected services or feature flags to stop further exposure while preserving system state for investigation.
3. Identify every tenant whose data may have been exposed and every tenant who may have viewed it. Record both sets separately in the incident ticket.
4. Do not delete, rotate, or overwrite logs, database snapshots, or access records until evidence preservation (below) is complete.
5. Confirm containment with the security lead before any customer-facing communication is drafted.
6. Document the exact start and end time of the exposure window as precisely as available data allows; this timestamp anchors the 72 hour notification clock.

## The 72 Hour Customer Notification Clock

1. The 72 hour clock starts at the moment Northwind Cloud confirms that customer data was actually exposed to an unauthorized party, not at the moment of first suspicion.
2. The security lead is responsible for setting and tracking this clock in the incident ticket.
3. Legal counsel must review and approve the notification content before it is sent, and this review must be scheduled early enough to meet the 72 hour deadline.
4. Affected customers are notified individually. Northwind Cloud does not disclose the identity or account details of any other affected customer in these notifications.
5. If root cause is not yet fully understood at the 72 hour mark, a preliminary notification is still sent describing known impact, with a follow-up to come once investigation concludes.

## Evidence Preservation

1. Upon SEV-1 or SEV-2 declaration, the on-call engineer takes a snapshot of relevant logs, database state, and access records before any remediation that could alter them, where feasible.
2. All preserved evidence is stored in the designated incident evidence bucket with restricted access limited to the security lead, legal counsel, and assigned investigators.
3. Chain-of-custody notes, including who accessed evidence and when, are logged in the incident ticket.
4. Evidence is retained for a minimum of one year following incident closure, or longer if legal counsel advises.

## What Support May and May Not Tell a Customer

Before the security lead approves a statement, support staff may:

1. Acknowledge that Northwind Cloud is investigating a reported issue and that the team takes it seriously.
2. Confirm that a ticket has been opened and provide a ticket number for tracking.
3. Tell the customer they will receive updates as information becomes available.

Support staff must never, before or after approval unless explicitly instructed otherwise by the security lead:

1. Confirm that a breach has occurred, in any wording, including informal terms like "hack" or "leak."
2. Share the name, account identifier, email domain, or any other identifying detail of any other customer, even in general terms such as "a company in your industry."
3. Speculate about root cause, scope, or timeline to the customer.
4. Promise specific remediation actions or compensation without security lead and legal sign-off.

All customer-facing statements about a security incident, written or verbal, must be approved by the security lead before release. Support should redirect detailed questions to the incident communication channel rather than answering directly.

## Post-Incident Review

1. A post-incident review is held within 10 days of incident closure for all SEV-1 and SEV-2 incidents.
2. The review covers detection time, containment time, root cause, notification timeline against the 72 hour clock, and any breakdown in the containment or communication process.
3. The security lead publishes a written summary to engineering leadership and support leadership within 2 business days of the review meeting.
4. Action items from the review are tracked to completion in the engineering backlog, with the security lead responsible for confirming closure.
