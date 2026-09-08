---
title: API Rate Limits and Fair Use
doc_id: api-rate-limits-2024
audience: customer
effective: 2024-03-01
supersedes: none
---

# API Rate Limits and Fair Use

This document describes the rate limits, webhook retry behavior, and fair use expectations that apply to all Northwind Cloud API integrations, effective 2024-03-01. It applies to every plan tier and covers both REST and webhook traffic.

## Rate Limits by Plan

Each API key is assigned a requests per minute (RPM) ceiling based on the account's subscription plan. Limits are enforced per key, not per account, so accounts with multiple keys can distribute load across them.

| Plan | Requests per Minute |
|------|----------------------|
| Starter | 30 |
| Pro | 300 |
| Enterprise | 1500 |

Requests that exceed the limit for a given plan receive an HTTP 429 response with a Retry-After header indicating how many seconds to wait before the next request. Clients should implement exponential backoff rather than retrying immediately, since repeated immediate retries count against the same rolling one minute window and can extend the throttling period.

## Webhook Delivery and Retries

Webhook events are delivered as soon as they are generated. If a webhook endpoint fails to respond with a 2xx status code, Northwind Cloud will retry delivery up to 3 attempts over a 30 minute window. The retry schedule is spaced to give integrators time to recover from transient outages: an initial retry shortly after the failure, a second retry roughly midway through the window, and a final retry near the end of the 30 minutes. If all 3 attempts fail, the event is marked as undelivered and will appear in the Webhook Delivery Log in the customer dashboard, where it can be manually replayed.

Webhook retries do not count against the RPM limits described above, since they are outbound calls from Northwind Cloud rather than inbound API requests from the customer.

## Measuring and Monitoring Usage

Every API response includes three headers to help clients track their consumption in real time:

1. X-RateLimit-Limit: the RPM ceiling for the key making the request.
2. X-RateLimit-Remaining: the number of requests left in the current one minute window.
3. X-RateLimit-Reset: the number of seconds until the window resets.

Customers integrating at scale are encouraged to build these headers into their client logic so that requests are paced proactively rather than relying solely on 429 responses. The API Usage panel in the customer dashboard also displays a rolling 24 hour view of request volume per key, which is useful for capacity planning ahead of product launches or seasonal traffic spikes.

## Fair Use Expectations

Rate limits define the maximum sustained throughput permitted per plan, but fair use also covers patterns of traffic that could degrade service for other customers even if they stay under the RPM ceiling. Examples of activity that may prompt a review include sustained polling at the maximum allowed rate when a webhook subscription would be more appropriate, or bursts of concurrent connections designed to work around per minute limits. Northwind Cloud reserves the right to contact accounts exhibiting these patterns before any limit is adjusted or enforcement action is taken.

Accounts that consistently operate near their plan's ceiling should consider upgrading to the next tier rather than distributing traffic across multiple keys, since the latter can complicate support and billing reconciliation.

## Requesting a Limit Increase

Enterprise customers with a documented need for throughput above 1500 requests per minute may request a custom limit through their account team. Requests are evaluated based on integration architecture, expected peak load, and historical usage patterns. Approved increases are applied to the specific API key requested and are reviewed periodically to confirm they still match actual usage.

## Changes to This Policy

Northwind Cloud will provide 6 months deprecation notice before reducing any published rate limit or materially changing webhook retry behavior for existing plans. Notice will be delivered by email to the account's designated technical contact and posted in the developer changelog. Increases to limits, new plan tiers, or additive features such as new webhook event types may be introduced without this notice period, since they do not reduce existing capability.

Customers with questions about their current limits, retry logs, or an upcoming limit change should contact their account team or open a support ticket through the customer dashboard.
