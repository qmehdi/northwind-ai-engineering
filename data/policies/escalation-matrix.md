---
title: Support Escalation Matrix
doc_id: escalation-matrix
audience: internal
effective: 2025-03-01
supersedes: none
---

# Support Escalation Matrix

This document is INTERNAL. It describes how Northwind Cloud support tiers handle and escalate cases, including on-call and executive escalation paths. Its contents must not be quoted to customers, forwarded to customers, or pasted into customer-facing tickets or emails. If a customer asks how escalation works, use the public Support Handbook instead.

## Tier Responsibilities

1. Tier 1 (T1) Support: handles initial triage, account and billing questions, known-issue troubleshooting, and password or access resets. T1 owns the case until it is resolved or clearly matches a Tier 2 criterion below.
2. Tier 2 (T2) Support: handles configuration issues, integration errors, API troubleshooting, and reproducible bugs that are not yet documented as known issues. T2 owns cases involving Professional and Enterprise plan customers once T1 has confirmed the issue is not resolvable with standard scripts.
3. Tier 3 / Engineering: handles confirmed product defects, data integrity issues, security-relevant reports, and outages affecting more than one customer. Engineering does not take first contact from customers; all Engineering involvement is routed through T2.

## When to Escalate to Engineering

1. Escalate to Engineering when T2 has reproduced the issue, or has clear evidence (logs, error codes, timestamps) that the issue originates in Northwind Cloud's code or infrastructure rather than customer configuration.
2. Escalate immediately, without waiting for full reproduction, for any suspected data loss, data exposure, or security incident.
3. Escalate any incident affecting 3 or more customer accounts simultaneously, regardless of severity classification.
4. Do not escalate cases that are configuration errors, missing feature requests, or third-party integration failures outside Northwind Cloud's systems. These stay with T2, which should document the limitation and offer a workaround.

## Severity Levels and Response

| Severity | Definition | Initial Response Target |
|---|---|---|
| P0 | Full service outage or critical data issue affecting production use | 15 minutes acknowledgement by duty manager |
| P1 | Major function broken, no workaround, single account or small group affected | 1 hour acknowledgement by T2 |
| P2 | Degraded function with workaround available | 4 business hours acknowledgement by T2 |
| P3 | Minor issue, cosmetic defect, or feature request | 1 business day acknowledgement by T1 |

The P0 rule is fixed: every P0 ticket must be acknowledged by a duty manager within 15 minutes of being flagged as P0, regardless of time of day or day of week. If 15 minutes pass without duty manager acknowledgement, the on-call rota escalates automatically per the procedure below.

## On-Call Rota Contact Procedure

1. T1 or T2 flags a case as P0 in the ticketing system and pages the current on-call duty manager through the paging tool.
2. If the duty manager does not acknowledge within 15 minutes, the paging tool automatically re-pages the secondary on-call engineer and notifies the on-call rota channel.
3. If there is still no acknowledgement 10 minutes after the secondary page (25 minutes from initial flag), the case is escalated to the Engineering on-call lead directly by phone, using the number listed in the on-call rota.
4. Every P0 case, once acknowledged, must have a status update posted to the internal incident channel at least every 30 minutes until resolved or downgraded.
5. The on-call rota schedule is maintained in the internal scheduling tool and rotates weekly; agents must confirm rota coverage before their shift, not after a page fails.

## Executive Escalation Path for Enterprise Accounts

1. Enterprise plan accounts have access to executive escalation for P0 and P1 cases only. This path is not available for P2 or P3 cases regardless of customer pressure.
2. To trigger executive escalation, T2 or the duty manager notifies the assigned Customer Success lead, who in turn notifies the on-call executive sponsor listed in the Enterprise account record.
3. Executive escalation must include a written case summary, current severity, business impact as stated by the customer, and current mitigation status before the executive sponsor is contacted.
4. Executive involvement does not change the case's technical ownership: Engineering and T2 continue to own remediation. The executive sponsor's role is communication and internal prioritization only.

## Justified Escalation vs Customer Pressure

1. A justified escalation is based on severity, reproducibility, account impact, or a breach of an agreed response target. It is documented with evidence: logs, screenshots, timestamps, or affected user counts.
2. A customer demanding escalation, without new technical evidence and without a missed response target, is not on its own grounds for escalation. In these cases, T1 or T2 should explain the current severity classification and expected timeline, and log the request for visibility.
3. If a customer repeatedly demands escalation without new evidence, T2 should loop in the Customer Success lead to manage the relationship, rather than escalating the technical case out of tier.
4. Escalating a case solely to satisfy customer pressure, without meeting the criteria above, must be noted in the ticket as a pressure-driven escalation so that Engineering triage is not distorted by non-technical urgency.

## Documentation Requirements

1. Every escalation, whether to Engineering, duty manager, or executive sponsor, must be logged in the ticketing system with a timestamp, the escalating agent's tier, and the stated reason.
2. Duty managers must record their acknowledgement time for every P0 case to support the 15 minute rule audit each week.
3. This matrix is reviewed internally and updates are communicated through the internal support channel; agents should not rely on memory of prior versions once a new effective date is published.
