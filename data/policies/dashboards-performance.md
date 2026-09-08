---
title: Dashboard Performance and Limits
doc_id: dashboards-performance
audience: customer
effective: 2024-12-05
supersedes: none
---

# Dashboard Performance and Limits

This document describes the operational limits that govern dashboard behavior on Northwind Cloud, including widget capacity by plan, query timeouts, export constraints, external sharing rules, and caching. These limits are designed to keep dashboards responsive for all customers sharing the platform's query infrastructure.

## Widget Limits by Plan

Each dashboard supports a maximum number of widgets depending on your subscription plan. This limit applies per dashboard, not per account, so you may create multiple dashboards to organize additional widgets.

| Plan | Maximum Widgets per Dashboard |
|------|-------------------------------|
| Starter | 8 |
| Pro | 20 |
| Enterprise | 40 |

If you attempt to add a widget beyond your plan's limit, the dashboard editor will block the action and display an upgrade prompt. Removing or consolidating widgets is the fastest way to stay within limits without upgrading. Enterprise customers who require more than 40 widgets on a single dashboard should contact their account manager to discuss a custom configuration.

## Query Timeouts

Every widget on a dashboard runs an underlying data query when the dashboard loads or refreshes. To protect shared infrastructure, all queries are subject to a 30 second timeout. If a query does not return results within 30 seconds, the widget will display a timeout error rather than partial or stale data.

Common causes of query timeouts include:

1. Filtering across very large date ranges, such as multiple years of raw event data.
2. Combining several high-cardinality dimensions in a single widget, such as user ID and timestamp together.
3. Running complex joins across multiple data sources within one widget.

Widgets that consistently time out should be simplified or rebuilt using pre-aggregated data sources where available.

## PDF Export Limits

Dashboards can be exported to PDF for offline sharing or reporting. Exports are limited to 20 pages per PDF file. Each dashboard's widgets are laid out across pages based on the dashboard's grid configuration, so the number of widgets and their arrangement will determine how many pages an export requires.

If a dashboard would generate a PDF longer than 20 pages, the export tool will only render the first 20 pages and will display a warning banner in the export preview. To keep exports within this limit:

1. Reduce the number of widgets on the dashboard before exporting.
2. Split large dashboards into smaller, topic focused dashboards.
3. Use the export tool's page selection option, where available, to export only the sections you need.

## External Sharing and Link Expiry

Dashboards can be shared externally using a shareable link. External links do not require the recipient to have a Northwind Cloud account, so access control is managed entirely through link settings.

Key rules for external sharing:

1. Shareable links expire automatically after 7 days by default. This default expiry period cannot be disabled, but it can be shortened or extended when creating the link, up to limits defined by your plan.
2. Expired links return an access denied page rather than dashboard data. A new link must be generated to restore access.
3. Links can optionally be protected with a password, and Enterprise plan customers can additionally restrict access by IP address range.
4. Anyone with a valid, unexpired link can view the dashboard in read-only mode. Recipients cannot edit widgets, change filters, or see underlying data sources.

We recommend reviewing active external links periodically and revoking any that are no longer needed, rather than relying solely on the 7 day default expiry.

## Caching Behavior

To reduce load on data sources and keep dashboards responsive, Northwind Cloud caches widget query results for a short period after each successful load. Cached results are served automatically when a dashboard is reopened within the cache window, which avoids re-running identical queries and reduces the chance of hitting the 30 second timeout.

Caching behavior details:

1. Cached results are typically served for up to 5 minutes before a fresh query is triggered on the next dashboard view.
2. Manually refreshing a dashboard bypasses the cache and forces a live query against the underlying data source.
3. Externally shared dashboards also use cached results, so recipients viewing a shared link may see data that is a few minutes old rather than a live, real-time query.

If you need near real-time data for a specific widget, consider isolating it on its own dashboard with fewer competing widgets, which reduces overall query load and allows more frequent manual refreshes.

## Tips for Slow Dashboards

If a dashboard is loading slowly or widgets are frequently timing out, consider the following:

1. Reduce the number of widgets per dashboard, especially if you are close to your plan's limit of 8, 20, or 40 widgets.
2. Narrow date ranges and filters on individual widgets rather than querying entire historical datasets.
3. Avoid stacking multiple high-cardinality breakdowns in a single widget; instead, split them across separate widgets or dashboards.
4. Rely on cached results where up to date data is not critical, and reserve manual refreshes for widgets that truly need live data.
5. For recurring reporting needs, export smaller, focused dashboards rather than one large dashboard, to stay within the 20 page PDF export limit and improve load times for viewers.

For further guidance on optimizing dashboard performance for your specific plan, contact Northwind Cloud support.
