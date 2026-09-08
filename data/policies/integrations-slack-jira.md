---
title: Slack and Jira Integrations
doc_id: integrations-slack-jira
audience: customer
effective: 2025-01-20
supersedes: none
---

# Slack and Jira Integrations

## Overview

Northwind Cloud supports two-way integrations with Slack and Jira Cloud so that alerts, comments, and status changes stay synchronized across your team's tools. This document explains how to connect each integration, the permissions required, why alerts sometimes stop, common causes of duplicate notifications, the rate limits Slack imposes on our app, how Jira projects map to Northwind workspaces, how to re-authorize a broken connection, and known limitations. The Slack integration is available on the Growth plan and above; the Jira integration is available on all paid plans.

## Setup and Required Scopes

To connect Slack, a workspace administrator must approve the following OAuth scopes during installation:

1. chat:write, to post alerts and replies into channels.
2. channels:read, to list available channels for routing.
3. users:read, to map Slack user IDs to Northwind account owners.
4. im:write, to send direct message notifications when a channel is not selected.

For Jira Cloud, the connecting user must have project administrator access and grant these scopes:

1. read:jira-work, to read issue and project metadata.
2. write:jira-work, to create and update issues from Northwind alerts.
3. read:jira-user, to attribute comments to the correct Jira account.

Both integrations require a workspace-level connection, meaning one authorization covers the entire Northwind organization rather than individual users. Only account owners or admins can initiate or revoke the connection.

## Jira Project Mapping

Each Northwind workspace maps to exactly one default Jira project, selected during setup. You may add up to 5 additional project mappings per workspace on the Business plan, and up to 20 on the Enterprise plan. Mappings are configured under Settings > Integrations > Jira > Project Rules, where you can route alerts by severity, tag, or service owner to a specific project and issue type. Issue type defaults to "Task" unless a custom mapping specifies "Bug" or "Incident." Changes to project mapping take effect immediately for new alerts but do not retroactively move existing linked issues.

## Why Alerts Stop

Alerts most commonly stop for one of these reasons:

1. Token revoked: a Slack or Jira admin manually revoked the app, or a Slack access token expired after 90 days of inactivity.
2. Channel archived: the destination Slack channel was archived, which disables posting even though the connection itself remains valid.
3. App reinstalled: someone reinstalled the Northwind app in Slack or reconnected Jira under a different account, which generates a new token and invalidates the old mapping.
4. Workspace or project deleted: the linked Jira project was deleted or the Slack workspace was deprovisioned.

When any of these occur, Northwind Cloud sends an email to the workspace owner within 15 minutes and displays a red status badge on the Integrations page.

## Duplicate Alerts

Duplicate notifications typically result from one of the following:

1. Multiple integration instances pointing to the same channel or project, often left over from a prior reinstallation.
2. Webhook retries: if Slack or Jira does not acknowledge delivery within 5 seconds, Northwind retries the request once, which can produce a second message if the first delivery actually succeeded but the acknowledgment was delayed.
3. Jira automation rules that independently trigger on the same issue transition Northwind Cloud already posted about.
4. Manual re-triggering of an alert rule within 30 seconds of the original, which our deduplication window does not suppress.

To reduce duplicates, remove unused integration instances under Settings > Integrations and confirm only one active connection exists per channel or project.

## Slack Rate Limits

Slack enforces API rate limits at the workspace level, and Northwind Cloud operates within them as follows:

| Limit type | Value |
|---|---|
| Messages per channel | 1 per second |
| API calls per workspace | 50 per minute |
| Burst allowance | up to 20 requests before throttling |

If your workspace exceeds these limits, Slack returns a 429 response and Northwind queues the alert for retry, delaying delivery by up to 60 seconds. Sustained high alert volume, such as more than 50 alerts per minute, may cause visible delays; consider batching or filtering alert rules if this occurs regularly.

## Re-authorization Steps

If an integration shows as disconnected or alerts have stopped, follow these steps:

1. Go to Settings > Integrations and locate the affected Slack or Jira connection.
2. Click Reconnect, which opens the provider's OAuth consent screen.
3. Sign in with an account that has admin rights in Slack or project administrator rights in Jira.
4. Approve the requested scopes listed above.
5. Confirm the channel or project mapping is still correct, since reconnection sometimes resets default routing.

Reconnection typically restores alert flow within 5 minutes. If alerts do not resume after 30 minutes, contact support with your workspace ID.

## Known Limitations

1. Only Jira Cloud is supported; Jira Server and Data Center are not compatible.
2. A single Slack message is limited to 4000 characters; longer alert payloads are truncated with a link to the full detail page.
3. File attachments sent through Slack are limited to 5MB per file.
4. A workspace may map at most 20 Jira projects, even on the Enterprise plan.
5. Comment sync between Jira and Slack is one-way, from Jira to Slack, not the reverse.
