---
title: Notifications and Alert Routing
doc_id: notifications-alerting
audience: customer
effective: 2024-10-15
supersedes: none
---

# Notifications and Alert Routing

This document explains how Northwind Cloud generates alerts, routes them to your team, and manages suppression, digests, and retention. It applies to all customers using the Alerts and Notifications module across Starter, Pro, and Enterprise plans, with plan-specific limits noted where relevant.

## Alert Rules

Alerts are generated when a monitored condition crosses a threshold you configure, such as error rate, latency, queue depth, or a custom metric pushed through the API. Each alert rule includes:

1. A trigger condition and evaluation window.
2. A severity level: informational, warning, or critical.
3. One or more notification channels.
4. Optional suppression and digest settings.

Rules are evaluated continuously. When a condition is met, Northwind Cloud creates an alert event and routes it according to the channels attached to that rule. You can attach multiple channels to a single rule, and a single channel can be used by multiple rules.

## Routing Channels

Supported channels are:

| Channel | Delivery method | Notes |
|---|---|---|
| Email | SMTP via Northwind mail relay | Supports individual recipients or distribution lists |
| Slack | Slack app integration | Requires workspace authorization and channel selection |
| Webhook | HTTPS POST to a customer-provided URL | Payload is JSON, includes alert ID, timestamp, and severity |

Each channel can be configured independently per rule. Webhook endpoints must respond with a 2xx status within 10 seconds; failed deliveries are retried up to 3 times before being marked as failed in the alert log.

## Digest Options

Instead of receiving every alert individually, you can configure a digest that batches alerts over a fixed interval and sends a single summary message. Digest options are available for email and Slack channels. Digests do not apply to webhook delivery, since webhook payloads are intended for programmatic consumption and are sent per event.

Digest intervals are configured per rule and can be set independently from suppression windows. A digest will still include every alert that fired during the interval, listed individually within the summary.

## Suppression Windows

Suppression windows prevent repeated notifications for the same alert condition within a defined period. This feature is available on Pro and Enterprise plans only. Starter plan customers receive a notification for every triggering event with no suppression option.

Suppression windows can be set from 1 to 60 minutes, in one-minute increments. During a suppression window:

1. The first occurrence of a matching alert is delivered normally.
2. Subsequent occurrences of the same alert condition within the window are logged but not delivered.
3. Once the window expires, the next matching occurrence is delivered and a new suppression window begins.

Suppression is evaluated per rule and per resource, so suppressing alerts for one service does not affect alerts for another service using the same rule.

## Why Duplicates Happen

Customers sometimes see what appear to be duplicate alerts. Common causes include:

1. **Multiple rules matching the same condition.** If two rules overlap in scope, both will fire independently.
2. **Multiple channels on one rule.** A single alert event delivered to email, Slack, and webhook simultaneously will appear three times, once per channel, but is a single underlying event.
3. **Suppression window not configured.** On Starter plans, or on Pro and Enterprise plans where suppression has not been set, repeated triggering of the same condition will generate a new notification each time it evaluates as true.
4. **Retry behavior on webhooks.** If a webhook endpoint returns a non-2xx response, Northwind Cloud retries delivery, which can appear as a duplicate if the original delivery ultimately succeeded after a delay.

If you are seeing more notifications than expected, check whether suppression windows are enabled and review overlapping rule conditions before contacting support.

## Unsubscribe Rules

Notification preferences can be managed per user and per channel. For email notifications, individual users can unsubscribe from non-critical alert categories through their notification settings or via the unsubscribe link included in each message.

Transactional emails cannot be unsubscribed. This includes account security notices, billing confirmations, and service-critical alerts required for operational awareness. These messages are sent regardless of notification preferences to ensure your team retains visibility into events that affect account access, billing status, or platform availability.

Slack and webhook channels are managed at the integration level by an account administrator rather than per user, since these channels typically represent shared team destinations rather than individual inboxes.

## Alert Retention

All alert events, including delivered, suppressed, and failed deliveries, are retained in the alert log for 90 days from the time of the triggering event. After 90 days, alert records are permanently deleted and cannot be recovered. This retention period applies uniformly across Starter, Pro, and Enterprise plans.

We recommend exporting alert history through the API or dashboard export function if you require retention beyond 90 days for audit or compliance purposes. Exported records are not automatically re-imported and must be stored by your organization according to your own data retention policy.

## Getting Help

If you need assistance configuring alert rules, suppression windows, or channel integrations, contact Northwind Cloud support through the in-app help widget or your assigned account representative. Enterprise customers with a dedicated technical account manager should route configuration questions through that channel for fastest response.
