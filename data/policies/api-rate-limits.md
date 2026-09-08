---
title: API Rate Limits and Fair Use
doc_id: api-rate-limits
audience: customer
effective: 2025-05-05
supersedes: api-rate-limits-2024
---

# API Rate Limits and Fair Use

This document describes the rate limits that apply to the Northwind Cloud API, how limit enforcement works, webhook retry behavior, our deprecation policy, and how to request a limit increase. It supersedes the previous policy api-rate-limits-2024, effective 2025-05-05.

## Plan Limits

Each API key is assigned a requests-per-minute (RPM) ceiling based on the workspace plan. Limits are enforced per key and, separately, per workspace, whichever threshold is reached first.

| Plan | Requests per minute | Burst allowance |
|---|---|---|
| Starter | 60 | None |
| Pro | 600 | None |
| Enterprise | 3000 | Short bursts above 3000 permitted for up to 10 seconds |

The Enterprise burst allowance is intended to absorb momentary traffic spikes, such as batch jobs or synchronized cron triggers, and is not a sustained increase to the base limit of 3000 requests per minute.

## Per-Key vs Per-Workspace Enforcement

1. Per-key limits apply to the individual API key making the request and reset on a rolling one-minute window.
2. Per-workspace limits aggregate usage across all keys in a workspace and use the same plan ceiling shown above.
3. If a workspace has multiple keys, the per-workspace limit is the binding constraint once combined traffic reaches the plan threshold, even if no single key has exceeded its own limit.
4. Current usage and remaining quota are returned on every response via the X-RateLimit-Limit, X-RateLimit-Remaining, and X-RateLimit-Reset headers.

## 429 Responses and Retry-After

When a request exceeds the applicable limit, the API returns HTTP status 429 Too Many Requests.

1. The response body includes an error code of rate_limited and a short description of which limit (per-key or per-workspace) was exceeded.
2. The response includes a Retry-After header, expressed in seconds, indicating the minimum wait time before the next request is likely to succeed.
3. Clients should implement exponential backoff on repeated 429 responses rather than retrying immediately, even if Retry-After suggests a short wait.
4. Repeated sustained 429 responses from a single key may trigger a temporary cooldown period on that key to protect overall API stability.

## Webhook Delivery Retries

Webhook delivery failures, including timeouts and non-2xx responses from your endpoint, are retried automatically.

1. Northwind Cloud attempts delivery up to 5 times total per event.
2. Retries are spread over a 2 hour window using exponential backoff between attempts.
3. If all 5 attempts fail, the event is marked as failed and is visible in the Webhooks Delivery Log in your workspace dashboard.
4. Failed events can be manually redelivered from the dashboard for up to 30 days after the original delivery attempt.
5. Webhook delivery attempts count toward your per-workspace rate limit only if you configure your endpoint on the same domain used for standard API calls; dedicated webhook receiver endpoints are exempt.

## Deprecation Policy

Northwind Cloud provides advance notice before removing or materially changing any API endpoint, field, or webhook event type.

1. A minimum of 12 months notice is given before any breaking change or endpoint sunset.
2. During the notice period, affected endpoints return a Sunset header indicating the planned removal date, along with a Deprecation header confirming the endpoint is deprecated.
3. Notices are also posted to the API changelog and sent to the technical contact on file for each workspace.
4. Non-breaking changes, such as adding new optional fields or new endpoints, are not subject to the 12 month notice requirement.

## Requesting a Limit Increase

1. Enterprise plan workspaces may request a custom rate limit above 3000 requests per minute by contacting your account team or submitting a request through the dashboard under Settings, API, Request Limit Increase.
2. Include your expected peak requests per minute, the nature of the workload (bulk sync, real-time integration, batch reporting, etc.), and the API endpoints involved.
3. Starter and Pro plan workspaces seeking limits above 60 or 600 requests per minute respectively should first evaluate whether an upgrade to the next plan tier meets their needs, since plan upgrades typically resolve limit constraints faster than a custom exception.
4. Approved increases are typically applied within 5 business days and are reflected automatically in the X-RateLimit-Limit header without requiring any change to your integration code.
5. Temporary limit increases for time-bound events, such as product launches or migrations, can also be requested and are granted for a defined window agreed with your account team.

## Summary

Rate limits exist to keep the API fast and reliable for all customers. Respecting the per-key and per-workspace thresholds in the table above, handling 429 responses with Retry-After and backoff, and monitoring the rate limit headers on every response will keep your integration resilient. For sustained growth beyond your current plan's limit, reach out early so an increase or plan change can be arranged before it affects production traffic.
