---
title: Security and Compliance Commitments
doc_id: customer-security-commitments
audience: customer
effective: 2025-04-15
supersedes: none
---

# Security and Compliance Commitments

Northwind Cloud is committed to protecting customer data through industry-standard technical controls, independent audits, and clear breach response practices. This document summarizes the security and compliance commitments that apply to all customers, with additional options available to customers on the Enterprise plan.

## Encryption Standards

1. Data at rest is encrypted using AES-256.
2. Data in transit is encrypted using TLS 1.2 or higher for all client, API, and internal service connections.
3. Encryption keys are managed through a centralized key management service with regular rotation and restricted administrative access.
4. Customers may not disable or downgrade encryption settings for production environments.

## Compliance Certifications

Northwind Cloud maintains the following independent attestations:

| Framework | Status |
|---|---|
| SOC 2 Type II | Current |
| ISO 27001 | Current |

Audit reports are available to customers under a mutual non-disclosure agreement. Requests for audit reports should be directed to the account team or through the security questionnaire process described below. Certifications are renewed on an annual audit cycle, and customers will be notified if the scope of a certification materially changes.

## Data Residency Options

Enterprise customers may select a data residency region for primary data storage. Available regions are:

1. us
2. eu
3. apac

Once a region is selected at contract setup, customer data at rest is stored within that region's infrastructure boundary. Backups and disaster recovery copies are retained within the same selected region unless the customer requests otherwise in writing. Customers on plans other than Enterprise are hosted in the default region assigned at account creation and do not have region selection available.

## Breach Notification

1. In the event of a confirmed security breach affecting customer data, Northwind Cloud will notify affected customers within 72 hours of confirming the incident.
2. Notification will be sent to the primary security or administrative contact on file for the account.
3. Initial notification will include known facts about the incident, affected systems or data categories, and immediate containment actions taken.
4. Follow-up updates will be provided as the investigation progresses until the incident is resolved.
5. Customers are responsible for keeping security contact information current in their account settings to ensure timely notification.

## Subprocessors

Northwind Cloud maintains a current list of subprocessors used to deliver the service, including cloud infrastructure providers, monitoring tools, and support platforms. This list is published on the Northwind Cloud trust center page and is updated whenever a new subprocessor is added or removed. Customers who wish to receive advance notice of subprocessor changes may subscribe to update notifications through the trust center or request this through their account team. Material changes to subprocessors handling regulated data categories will be communicated directly to affected customers in addition to the published update.

## Penetration Testing

1. Northwind Cloud engages an independent third-party firm to conduct penetration testing at least annually.
2. Testing covers production application environments, API endpoints, and supporting infrastructure.
3. Identified findings are triaged and remediated according to internal severity timelines, with critical findings prioritized for immediate remediation.
4. A summary of the most recent penetration test results, excluding sensitive technical detail, is available to customers upon request under the same terms as audit reports.
5. Customers with contractual rights to conduct their own security testing must coordinate scheduling and scope with the Northwind Cloud security team in advance to avoid service disruption.

## Requesting the Security Questionnaire

Customers who need to complete internal vendor risk assessments may request the Northwind Cloud standard security questionnaire package, which includes:

1. Completed responses to common vendor security questionnaire formats.
2. Summaries of SOC 2 Type II and ISO 27001 certification status.
3. Subprocessor list reference and data residency details relevant to the customer's plan.

To request the questionnaire package, customers should contact their account team or submit a request through the support portal under the category "Security and Compliance." Requests are typically fulfilled within 5 business days. Customers requiring a signed non-disclosure agreement before receiving supporting audit documentation should note this in the request so the appropriate agreement can be routed for signature.

Questions about any commitment described in this document, including data residency setup for Enterprise accounts or breach notification contacts, should be directed to the account team or the security contact listed in the customer's service agreement.
