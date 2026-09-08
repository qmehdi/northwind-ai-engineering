---
title: Data Pipelines: Scheduling, Failures and Retention
doc_id: data-pipelines-ops
audience: customer
effective: 2025-03-10
supersedes: none
---

# Data Pipelines: Scheduling, Failures and Retention

This document describes how Northwind Cloud schedules, retries, and monitors data pipeline syncs, how memory is allocated per transform by plan, and how run logs and service credits are handled. It applies to all customers using the Data Pipelines module, effective 2025-03-10.

## Scheduling Semantics

1. Pipelines run on a schedule you define per connector: hourly, daily, or a custom cron expression.
2. All scheduled times are evaluated in UTC, regardless of the time zone shown in your account settings.
3. The minimum interval between two scheduled runs of the same pipeline is 15 minutes. Requests for tighter intervals are rejected at save time.
4. If a run is still in progress when the next scheduled run is due, the new run is queued and starts as soon as the prior run completes or fails.
5. Schedule changes take effect on the next calculated run time; they do not retroactively affect a run already queued or executing.

## Retry Policy for Failed Syncs

1. A failed sync is automatically retried up to 3 times before being marked as a hard failure.
2. Retries are spaced using exponential backoff: 2 minutes, 10 minutes, and 30 minutes after the prior attempt.
3. Retries only apply to transient failures such as connector timeouts, rate limiting responses, or temporary network errors.
4. Failures caused by invalid credentials, schema mismatches, or malformed transform logic are not retried automatically; these require you to correct the underlying configuration and trigger a manual run.
5. After the 3 automatic retries are exhausted, the pipeline status is set to "Failed" and an alert is sent to the notification channels configured for that pipeline (email, webhook, or both).

## Memory Limits by Plan

Each transform step in a pipeline runs within a memory limit determined by your subscription plan. If a transform exceeds its limit, the run fails with an out-of-memory error and is subject to the standard retry policy described above.

| Plan | Memory limit per transform |
|------------|-----------------------------|
| Starter | 2 GB |
| Pro | 8 GB |
| Enterprise | 32 GB |

If your workload regularly approaches these ceilings, consider splitting large transforms into smaller steps or upgrading your plan. Memory limits apply per transform, not per pipeline, so a pipeline with multiple transform steps can use more total memory across the run.

## Late-Arriving Data Handling

1. Records that arrive after their scheduled sync window closes are treated as late-arriving data and are captured in the next scheduled run for that connector.
2. Late-arriving data is merged using the same deduplication and upsert logic as on-time data; no separate reconciliation step is required from you.
3. If a source system backfills historical records outside the normal sync window, we recommend triggering a manual run to ensure the backfill is captured promptly rather than waiting for the next scheduled cycle.
4. Pipelines configured with append-only destinations will retain both the original and late-arriving records as separate rows; pipelines configured for upsert destinations will overwrite prior records using the configured primary key.

## Connector Credential Rotation

1. Connector credentials (API keys, OAuth tokens, database passwords) can be rotated at any time from the connector settings page without deleting the pipeline configuration.
2. Rotating credentials does not interrupt an in-progress run; the new credentials take effect starting with the next run.
3. For OAuth-based connectors, token refresh happens automatically; manual rotation is only required if you revoke access on the source system side.
4. We strongly recommend rotating database and API credentials at least every 90 days as a security best practice, even though the platform does not enforce automatic expiry.

## Manual Run Triggers

1. You can trigger a manual run at any time from the pipeline detail page by selecting "Run now."
2. Manual runs use the same memory limits, retry policy, and logging behavior as scheduled runs.
3. Manual runs do not affect the underlying schedule; the next scheduled run still occurs at its originally calculated time.
4. Manual runs are limited to 10 per pipeline per day to prevent unintended load on source systems; this limit resets at midnight UTC.

## Run Logs, Retention, and Refund Policy

1. Run logs, including status, duration, row counts, and error messages, are retained for 30 days from the run completion time.
2. After 30 days, run logs are permanently deleted and cannot be recovered; export logs before this window closes if you need them for auditing.
3. A failed sync, whether due to source system errors, credential issues, schema changes, or exhausted retries, is not eligible for a refund or service credit.
4. Our Service Level Agreement covers platform availability only, meaning uptime of the scheduling, execution, and orchestration infrastructure. It does not guarantee successful completion of any individual sync, since sync success depends on the availability and correctness of your source and destination systems.
5. If you believe a failure was caused by a platform outage rather than a source or configuration issue, contact support with the pipeline ID and approximate failure time so we can review the incident against platform status records.
