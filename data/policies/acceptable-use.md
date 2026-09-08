---
title: Acceptable Use Policy
doc_id: acceptable-use
audience: customer
effective: 2024-06-01
supersedes: none
---

# Acceptable Use Policy

This Acceptable Use Policy applies to all customers, authorized users, and integrations that access Northwind Cloud services, including the API, dashboard, and storage infrastructure. It governs how accounts, data, and computing resources may be used and describes the consequences of violations. This policy is incorporated into your Master Subscription Agreement and takes effect on 2024-06-01.

## Purpose and Scope

Northwind Cloud provides infrastructure for business applications on the Starter, Growth, and Enterprise plans. This policy applies to every account regardless of plan tier, and to every user, service account, or API key associated with that account. It covers direct use of the platform as well as use through third-party applications connected via our API.

## Prohibited Uses

The following activities are prohibited on all Northwind Cloud services:

1. Uploading, storing, or transmitting malware, ransomware, or any code designed to disrupt or gain unauthorized access to systems.
2. Using the platform to send unsolicited bulk communications, phishing content, or fraudulent messages.
3. Attempting to bypass authentication controls, rate limits, or storage quotas through automated workarounds.
4. Reverse engineering, scraping, or reselling Northwind Cloud services without a written reseller agreement.
5. Storing content that infringes intellectual property rights or violates applicable law, including export control regulations.
6. Using shared infrastructure to mine cryptocurrency or run workloads unrelated to your business use case.

Violations of this section may result in immediate suspension, as described under Enforcement and Warning Process.

## Fair Use of API and Storage

All plans are subject to fair use limits to ensure consistent performance across the platform.

| Plan | API Rate Limit | Storage Included | Overage Handling |
|------|-----------------|-------------------|-------------------|
| Starter | 600 requests per minute | 50 GB | Throttled at limit |
| Growth | 2,400 requests per minute | 500 GB | Billed at standard overage rate |
| Enterprise | 10,000 requests per minute | 5 TB | Custom terms per contract |

Requests exceeding the per-minute limit receive an HTTP 429 response and are queued for up to 60 seconds before being dropped. Storage usage is calculated daily, and accounts exceeding their included allocation by more than 10 percent for 3 consecutive days will receive an automated notice with options to upgrade or reduce usage. Sustained abuse of shared compute resources, defined as consistent utilization above 90 percent of allocated capacity for more than 24 hours without a corresponding plan upgrade, may trigger a fair use review by our infrastructure team.

## Account Sharing and Authorized Users

Each Northwind Cloud account is licensed to a single organization and its designated authorized users.

1. Login credentials must not be shared across separate legal entities or unrelated business units.
2. Each named user must have a unique login; shared generic logins such as "admin@company.com" used by more than one person are not permitted.
3. API keys are scoped to a single account and must not be distributed to third parties outside your organization without prior written consent from Northwind Cloud.
4. Enterprise plan customers may request up to 25 named user seats per workspace at no additional charge; additional seats are billed per the applicable order form.
5. Service accounts used for automated integrations must be labeled clearly in the dashboard and reviewed at least once every 90 days.

Account sharing that circumvents per-seat billing is treated as a billing violation and may result in retroactive invoicing in addition to the enforcement steps below.

## Enforcement and Warning Process

Northwind Cloud uses a graduated enforcement process for most violations, except in cases of severe abuse such as malware distribution or active security threats, which result in immediate suspension without prior warning.

1. **First warning**: Email notice sent to the account owner describing the violation, with 5 business days to remediate.
2. **Second warning**: If unresolved after 5 business days, a formal notice is issued along with a 48 hour remediation window and possible feature restrictions.
3. **Third warning**: Failure to remediate within 48 hours results in account suspension for up to 30 days.
4. **Termination**: Repeated violations within a rolling 12 month period, or failure to remediate during a suspension, may result in permanent termination of the account under the terms of your Master Subscription Agreement.

Customers may appeal any enforcement action by contacting their account representative or support@northwindcloud.com within 10 business days of the notice. During an active investigation, Northwind Cloud may temporarily restrict API access or export functions to prevent further harm while preserving your ability to retrieve your own data.

## Reporting Abuse

If you become aware of activity that violates this policy, whether originating from your own account, another customer, or an external party, report it promptly.

1. Email abuse@northwindcloud.com with a description of the issue, relevant account identifiers, timestamps, and any supporting logs or screenshots.
2. For active security incidents, such as suspected data breaches or unauthorized access, mark the subject line "Urgent Security" to trigger priority review.
3. Our trust and safety team acknowledges all reports within 1 business day and provides a resolution update within 5 business days for standard cases.
4. Anonymous reports are accepted, but providing contact details allows us to follow up for clarification and to share resolution status.

Northwind Cloud reserves the right to update this Acceptable Use Policy periodically. Material changes will be communicated to account owners through the dashboard or by email prior to taking effect.
