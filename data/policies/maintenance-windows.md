---
title: Scheduled Maintenance
doc_id: maintenance-windows
audience: customer
effective: 2024-09-01
supersedes: none
---

# Scheduled Maintenance

## Overview

Northwind Cloud performs routine infrastructure maintenance to keep the platform secure, stable, and performant. This document describes when maintenance normally occurs, how we notify customers, what is excluded from uptime calculations, and how regional data residency affects scheduling. This policy applies to all Northwind Cloud production environments unless a separate contractual agreement states otherwise.

## Standard Maintenance Windows

1. Standard maintenance is scheduled for Sundays, 02:00 to 06:00 UTC.
2. Most maintenance activities are completed within 30 minutes of the window opening, but the full four hour window is reserved to allow for verification, rollback, and staged deployment across services.
3. Not every Sunday window is used. Maintenance is only performed when a change is required, such as a security update, database upgrade, or infrastructure migration.
4. Customers with workloads sensitive to brief connection interruptions should plan for possible short disruptions during this window, even though most maintenance is designed to be non disruptive.

## Notice Periods

1. Standard maintenance: customers receive at least 7 days advance notice before the scheduled window.
2. Urgent security patches: when a vulnerability requires immediate remediation, Northwind Cloud may proceed with only 24 hours notice.
3. Notices are sent by email to the account's designated technical contacts and posted on the status page described below.
4. Where possible, notices include the affected services, expected impact, and estimated duration.

## Status Page and Notifications

1. All scheduled and in progress maintenance is published on the Northwind Cloud status page at status.northwindcloud.com.
2. The status page shows the maintenance window start and end times in UTC, the services affected, and real time updates during the maintenance activity.
3. Customers can subscribe to status page updates by email or webhook to receive automated notifications matching the notice periods above.
4. In addition to the status page, an in-app banner is displayed to logged in users starting at the beginning of the 7 day standard notice period, or immediately for urgent security patches.

## Uptime Exclusions

The following are excluded from uptime and service level calculations:

1. Scheduled maintenance performed within the Sunday 02:00 to 06:00 UTC standard window, provided the 7 day notice period was met.
2. Urgent security patches performed with at least 24 hours notice, regardless of day or time.
3. Downtime caused by factors outside Northwind Cloud's control, including customer network issues, third party service outages, and denial of service attacks mitigated in cooperation with the customer.
4. Maintenance performed at a customer's specific request, including custom migration or configuration work scheduled outside standard windows.

Any maintenance that falls outside these exclusions, or that exceeds its published window without adequate notice, is counted against uptime under the applicable service level agreement.

## Regional Maintenance Windows

Customers with data residency requirements are served from region specific infrastructure, and maintenance is scheduled independently for each region to reduce the chance of simultaneous impact across residency zones.

| Residency region | Maintenance window | Notice period |
|---|---|---|
| Global standard | Sundays, 02:00 to 06:00 UTC | 7 days standard, 24 hours for urgent security patches |
| EU residency | Sundays, 02:00 to 06:00 UTC (EU region infrastructure) | 7 days standard, 24 hours for urgent security patches |
| APAC residency | Sundays, 02:00 to 06:00 UTC (APAC region infrastructure) | 7 days standard, 24 hours for urgent security patches |

Although EU and APAC residency environments run on separate infrastructure from the global standard environment, they follow the same Sunday 02:00 to 06:00 UTC window and the same notice periods, 7 days for standard maintenance and 24 hours for urgent security patches. This alignment simplifies planning for customers operating across multiple regions while still isolating the underlying infrastructure changes per residency zone.

## Emergency Security Patches

1. When a critical vulnerability is identified, Northwind Cloud reserves the right to apply an emergency security patch with only 24 hours notice, even outside the standard Sunday window.
2. Emergency patches follow the same notification channels as standard maintenance: email to technical contacts, status page posting, and in-app banner.
3. Emergency patches are excluded from uptime calculations under the same terms as standard maintenance, provided the 24 hour notice period is met.
4. If an emergency patch requires downtime with less than 24 hours notice due to an active exploit, Northwind Cloud will provide a post incident report within a reasonable time after resolution, including the reason immediate action was required.

## Contact and Escalation

Customers with questions about a specific maintenance notice, or who need to request a change to a scheduled window for their environment, should contact their account team or open a support ticket referencing the maintenance notice date and affected services. For real time status during an active maintenance window, refer to status.northwindcloud.com before contacting support.
