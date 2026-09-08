---
title: Product and API Deprecations
doc_id: deprecations-2025
audience: customer
effective: 2025-07-01
supersedes: none
---

# Product and API Deprecations

## Overview

Northwind Cloud maintains a predictable deprecation process so customers can plan migrations without disruption to production systems. This document lists all currently announced deprecations, the migration resources available for each, what to expect once a sunset date passes, and how eligible customers can request an extension. This policy applies to all plans: Starter, Professional, and Enterprise, unless otherwise noted.

## Current Deprecation Schedule

| Feature | Deprecation Notice | Sunset Date | Replacement |
|---|---|---|---|
| API v1 | Effective 2025-07-01 | 2026-06-30 | API v2 |
| Legacy webhook signature | Effective 2025-07-01 | 2026-03-31 | HMAC-SHA256 webhook signing |
| SMS MFA | Effective 2025-07-01 | 2026-01-31 | Authenticator app or hardware key MFA |

All three items are considered final. No further extensions to these published sunset dates will be granted beyond the terms described in the Requesting an Extension section below.

## Notification Schedule

To give customers adequate lead time, Northwind Cloud sends deprecation reminders through account email and the account dashboard banner at three intervals before each sunset date:

1. 12 months before sunset: initial announcement and migration guide publication.
2. 90 days before sunset: reminder with usage report showing any accounts still relying on the deprecated feature.
3. 30 days before sunset: final reminder with a direct link to migration support resources.

Accounts that show continued usage of a deprecated feature within 30 days of sunset will also receive a direct outreach email from Customer Success.

## Migration Guides

Step-by-step migration guides are published for each deprecation and are available at docs.northwindcloud.com under the Migrations section:

1. API v1 to API v2 migration guide: covers endpoint mapping, authentication changes, and pagination differences between API v1 and API v2.
2. Legacy webhook signature migration guide: covers switching webhook consumers from the legacy signature scheme to HMAC-SHA256 webhook signing, including sample verification code in common languages.
3. SMS MFA migration guide: covers enrolling users in authenticator app or hardware key MFA and communicating the change to end users ahead of the 2026-01-31 end of support date.

Each guide includes a compatibility checklist and a sandbox environment link so customers can validate changes before the sunset date takes effect.

## What Happens After Sunset

Once a sunset date passes, the deprecated feature is fully retired and is no longer available in production or sandbox environments:

1. API v1 requests made after 2026-06-30 will receive an HTTP 410 Gone response with no data payload.
2. Webhooks signed using the legacy signature scheme after 2026-03-31 will be rejected by Northwind Cloud's outbound delivery system, and delivery attempts will stop after repeated verification failures.
3. SMS MFA after 2026-01-31 will no longer be offered as a login option; any accounts still configured with SMS MFA will be prompted to re-enroll in authenticator app or hardware key MFA at next login.

Northwind Cloud does not restore access to a deprecated feature after its sunset date except through the extension process described below.

## Requesting an Extension

Extensions are available only to customers on the Enterprise plan and only for the deprecations listed in this document.

1. Enterprise customers may request an extension of up to 90 days past the published sunset date for a given deprecation.
2. Extension requests must be submitted through the Enterprise support channel at least 30 days before the relevant sunset date.
3. Each extension request is reviewed individually and approved at Northwind Cloud's discretion based on migration progress and technical constraints described in the request.
4. Approved extensions are documented in writing with a confirmed new end date, which will not exceed 90 days beyond the original sunset date.
5. Starter and Professional plan customers are not eligible for extensions and should complete migration before the published sunset date.

Customers who anticipate needing more time should begin migration planning as early as possible after the 12 month notice, since extension approval is not guaranteed and is capped at 90 days.

## Support and Questions

For help planning a migration, contact Northwind Cloud support through the in-app help widget or the support email listed in your account dashboard. Enterprise customers should route extension requests and complex migration questions through their assigned Customer Success contact rather than general support, to ensure requests are logged against the correct account and reviewed within the required timelines.
