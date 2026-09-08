---
title: GDPR, Data Residency and Subprocessors
doc_id: gdpr-data-residency
audience: customer
effective: 2025-05-20
supersedes: none
---

# GDPR, Data Residency and Subprocessors

This document explains how Northwind Cloud handles personal data under the General Data Protection Regulation (GDPR), the data residency options available to European Union (EU) customers, and the processes governing subprocessors and cross-border data transfers. It applies to all customers processing personal data through the Northwind Cloud platform.

## Roles and Responsibilities

1. For personal data submitted by customers into the Northwind Cloud platform, Northwind Cloud acts as a data processor, and the customer acts as the data controller.
2. Customers are responsible for determining the legal basis for processing, obtaining any required consents, and responding to controller-level obligations under GDPR.
3. Northwind Cloud processes personal data only on documented instructions from the customer, as set out in the Data Processing Agreement (DPA) and the applicable service order.
4. Northwind Cloud maintains technical and organizational measures appropriate to the risk, including encryption in transit and at rest, access controls, and regular security reviews.

## Data Processing Agreement

1. A standard DPA is available to all customers and incorporates the GDPR-mandated processor obligations, including confidentiality, breach notification, and audit rights.
2. The DPA can be downloaded and countersigned through the Northwind Cloud Trust Portal without requiring a separate legal review cycle.
3. Customers on any plan may request a fully executed copy of the DPA by contacting privacy@northwindcloud.com.
4. In the event of a confirmed personal data breach affecting customer data, Northwind Cloud will notify the affected customer within 72 hours of becoming aware of the breach, consistent with the terms of the DPA.

## EU Data Residency Guarantees

1. Customers who select the EU Data Residency configuration have their production data, including primary storage and backups, stored and processed exclusively within the EU region.
2. Compute, storage, and database services supporting EU-resident accounts run on infrastructure located in EU data centers; no production personal data for these accounts is replicated outside the EU region.
3. Metadata strictly necessary for account administration, billing, and platform-wide security monitoring may be processed centrally, but this metadata does not include customer content data.
4. All support engineer access to EU-resident customer environments is logged, including the identity of the accessing engineer, timestamp, and the specific resource accessed. Access logs are retained for 90 days and available to customers on request.
5. EU Data Residency is available as an add-on for customers on the Enterprise plan and is enabled at account provisioning; customers should confirm residency configuration with their account team before go-live.

## Subject Access Requests

1. Where Northwind Cloud receives a data subject access request directly (for example, from an end user of a customer's application), Northwind Cloud will forward the request to the relevant customer within 5 business days, since the customer is the controller responsible for substantive response.
2. Where the customer submits a request to Northwind Cloud for assistance in fulfilling a data subject access request, Northwind Cloud will provide the requested data extract or confirmation within 30 days of receiving a complete and verified request.
3. Requests should be submitted through the Northwind Cloud Trust Portal or by email to privacy@northwindcloud.com, and must identify the customer account and the scope of data sought.
4. Northwind Cloud may extend the 30 day period once, by up to 30 additional days, for requests of particular complexity, with written notice to the customer explaining the reason for delay.

## Subprocessors and Change Notice

1. Northwind Cloud maintains a current list of subprocessors engaged to support the platform, published on the Trust Portal, including the subprocessor name, the service provided, and the processing location.
2. Before engaging a new subprocessor or materially changing the role of an existing one, Northwind Cloud will provide customers with 30 days advance notice via the Trust Portal and email notification to the account's designated security contact.
3. Customers may object to a new subprocessor in writing within the 30 day notice period. Northwind Cloud will work in good faith to address the objection, which may include offering an alternate configuration or, where no resolution is reached, permitting the customer to terminate the affected service without penalty.
4. All subprocessors are contractually bound to data protection obligations equivalent to those in the Northwind Cloud DPA.

## International Data Transfers

1. Where personal data is transferred outside the EU, for example for customers not enrolled in EU Data Residency, Northwind Cloud relies on the European Commission's Standard Contractual Clauses (SCCs) as the transfer mechanism, incorporated by reference into the DPA.
2. Northwind Cloud conducts transfer impact assessments for jurisdictions receiving personal data and applies supplementary technical measures, including encryption and pseudonymization, where warranted.
3. Customers enrolled in EU Data Residency are not subject to routine international transfers of production personal data, other than the limited platform metadata described above.

## Contact and Further Information

1. Questions regarding this policy, the DPA, subprocessor list, or data residency configuration should be directed to privacy@northwindcloud.com.
2. Copies of the subprocessor list, DPA template, and this policy are maintained on the Northwind Cloud Trust Portal and updated as changes occur.
3. This policy is reviewed periodically and customers are encouraged to check the Trust Portal for the current version.
