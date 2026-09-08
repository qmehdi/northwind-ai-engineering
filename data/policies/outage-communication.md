---
title: Outage Communication Standards
doc_id: outage-communication
audience: customer
effective: 2025-03-01
supersedes: none
---

# Outage Communication Standards

This document explains how Northwind Cloud communicates with customers during service incidents. It covers update frequency, timing commitments, post-incident reporting, root cause language, and how service credits are handled. These standards apply to all customers on the Starter, Growth, and Enterprise plans, with additional accommodations noted for Enterprise below.

## Severity Definitions

Northwind Cloud classifies incidents into two primary severity levels for the purposes of this policy:

1. P0: A critical incident causing full service outage or a major feature being completely unavailable for most customers.
2. P1: A significant incident causing degraded performance, partial functionality loss, or an outage affecting a limited subset of customers.

Lower severity issues, such as minor bugs or isolated single-account problems, are tracked through standard support channels and are not subject to the status page cadence described in this document.

## Status Page Update Cadence

During an active P0 incident, Northwind Cloud commits to the following communication timeline:

1. First update within 15 minutes of the incident being confirmed and declared internally.
2. Subsequent updates posted every 30 minutes for the duration of the P0 incident, even if the update simply states that investigation is continuing and no new information is available.
3. A final resolution update once the incident is confirmed fixed and monitoring has verified stability.

For P1 incidents, updates are posted at a reduced but still regular cadence, with the first update generally within 30 minutes and subsequent updates as meaningful progress is made or at least once per hour.

All updates are published on the Northwind Cloud status page and mirrored through the in-app notification banner for signed-in users. Customers can also subscribe to email or SMS alerts directly from the status page to receive these updates automatically as they are posted.

## Post-Incident Reports

After an incident is resolved, Northwind Cloud prepares a post-incident report summarizing what happened, the customer impact, and the corrective actions taken. Delivery timelines are as follows:

| Severity | Standard Delivery Window |
|----------|---------------------------|
| P0 | Within 5 business days of resolution |
| P1 | Within 10 business days of resolution |

Enterprise plan customers may request an expedited or more detailed post-incident report ahead of these standard windows by contacting their account team or support representative. Requests for expedited reports are accommodated on a best effort basis and do not change the underlying investigation timeline, only the delivery of an interim summary.

Post-incident reports are distributed via email to designated account contacts and, where applicable, published in a redacted form on the status page for broader visibility.

## Root Cause Language Guidelines

To keep post-incident reports clear, consistent, and useful, Northwind Cloud follows these guidelines when describing root causes:

1. Root causes are described in plain, factual language, avoiding vague terms such as "a glitch" or "technical issue" without further explanation.
2. Reports distinguish between the triggering event, the underlying root cause, and any contributing factors that extended impact or delayed detection.
3. Where the root cause involves a third-party dependency, such as a cloud infrastructure provider or upstream service, this is stated explicitly along with the mitigation Northwind Cloud has put in place or is planning.
4. Reports avoid speculative language once a root cause is confirmed. If a root cause remains under investigation at the time a report is due, the report states this clearly and commits to a follow-up update.
5. Internal team names, individual employee names, and internal tooling details are omitted from customer-facing reports to keep the focus on impact and resolution rather than internal process.

## Service Credits

Customers impacted by a qualifying P0 or P1 incident may be eligible for service credits under the terms of their applicable service level agreement. This document does not set out credit eligibility or calculation, which are governed separately. Customers should refer to their SLA policy or contact their account representative to determine eligibility and initiate a credit request. Credit requests are generally reviewed alongside the relevant post-incident report to confirm the incident severity and duration used in the calculation.

## Customer Responsibilities

To get the most value from these communication standards, customers are encouraged to:

1. Subscribe to status page notifications for the components and regions relevant to their deployment.
2. Designate at least one account contact authorized to receive post-incident reports and credit correspondence.
3. Report suspected outages promptly through support channels even if the status page has not yet reflected an issue, since early reports help reduce the time to the first update.

Northwind Cloud reviews this policy periodically and will communicate any material changes to the update cadence, reporting windows, or credit process pointer in advance of the change taking effect.
