# Identity and observability. One notification channel, log-based metrics for the signals the
# platform emits (drift alerts from every service and agent, Model Monitoring anomalies,
# pipeline errors, gateway budget hits), alert policies on them and on the gateway's health,
# a dashboard with one row per platform area, and the budget. Identity-Aware Proxy, Security
# Command Center and Organization Policy are documented in the README and listed in the
# `organization_policies` output; the course applies none of them because it has no
# organisation node.
variable "project" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "tenants" { type = list(string) }
variable "alert_email" { type = string }
variable "billing_account" { type = string }
variable "budget_usd" { type = number }
variable "gateway_service" { type = string }
variable "live_endpoints" { type = map(string) }

locals {
  services_regex = "^${var.environment}-.*-(triage|semantic|policy|agent|mcp)$"
  channels       = [for c in google_monitoring_notification_channel.email : c.id]
  # Constraints a platform team sets at the folder that holds every environment. Listed, not
  # applied: the course runs in one project without an organisation.
  organization_policies = [
    "constraints/iam.disableServiceAccountKeyCreation",
    "constraints/iam.automaticIamGrantsForDefaultServiceAccounts",
    "constraints/storage.uniformBucketLevelAccess",
    "constraints/storage.publicAccessPrevention",
    "constraints/run.allowedIngress",
    "constraints/compute.requireOsLogin",
    "constraints/gcp.resourceLocations",
    "constraints/sql.restrictPublicIp",
  ]
}

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  project      = var.project
  display_name = "${var.environment} alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

# ----- log-based metrics ---------------------------------------------------------------------

resource "google_logging_metric" "drift_alerts" {
  project     = var.project
  name        = "${var.environment}-drift-alerts"
  description = "drift_alert lines from every ${var.environment} service on Cloud Run and every agent on Agent Engine"
  filter      = "((resource.type=\"cloud_run_revision\" AND resource.labels.service_name=~\"${local.services_regex}\") OR resource.type=\"aiplatform.googleapis.com/ReasoningEngine\") AND jsonPayload.msg=\"drift_alert\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key         = "tenant"
      value_type  = "STRING"
      description = "NW_TENANT of the emitting service"
    }
  }
  label_extractors = {
    tenant = "EXTRACT(jsonPayload.tenant)"
  }
}

# Online quality (nw/quality.py and the monitors in nw/*/monitor.py): every service and agent logs
# one `quality_alert` line per signal and minute while a signal is past its bar, with `signal`
# (`shadow_agreement`, `p0_share`, `refusal_rate`, `judge_score`), `value`, `bar`, `window`,
# `service` and `tenant`. The line name is the contract with the monitors; the alert below and
# the canary check read this metric.
resource "google_logging_metric" "quality_alerts" {
  project     = var.project
  name        = "${var.environment}-quality-alerts"
  description = "quality_alert lines from every ${var.environment} service and agent"
  filter      = "((resource.type=\"cloud_run_revision\" AND resource.labels.service_name=~\"${local.services_regex}\") OR resource.type=\"aiplatform.googleapis.com/ReasoningEngine\") AND jsonPayload.msg=\"quality_alert\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key         = "tenant"
      value_type  = "STRING"
      description = "NW_TENANT of the emitting service"
    }
    labels {
      key         = "signal"
      value_type  = "STRING"
      description = "The quality signal that crossed its bar"
    }
  }
  label_extractors = {
    tenant = "EXTRACT(jsonPayload.tenant)"
    signal = "EXTRACT(jsonPayload.signal)"
  }
}

# The quality canary signals as numbers: the `metrics_snapshot` line every service writes each
# minute (nw/metrics_export.py, NW_METRICS_FORMAT=json) carries the current value of each gauge
# under the same field name. One distribution metric per field, labelled by service and tenant;
# the bars are deploy/SLO.md's. `quality_level` at 2 is the monitor's own verdict (it already
# holds the minimum samples and the interval) and is the one a canary waits on; the others say
# which signal moved. `p0_share` and `refusal_rate` are reported, not alarmed: the bars are on
# their ratios.
locals {
  quality_series = {
    quality_level    = { services = "triage|semantic|policy|agent", bounds = [-1, 0, 1, 2, 3], bars = { level = { comparison = "COMPARISON_GE", threshold = 2, aligner = "ALIGN_PERCENTILE_99" } } }
    shadow_agreement = { services = "triage|semantic", bounds = [0.5, 0.8, 0.9, 0.95, 0.99, 1], bars = { shadow = { comparison = "COMPARISON_LT", threshold = 0.9, aligner = "ALIGN_PERCENTILE_05" } } }
    p0_share_ratio = { services = "triage|semantic", bounds = [0.25, 0.5, 0.75, 1, 1.5, 2, 3], bars = {
      p0_high = { comparison = "COMPARISON_GE", threshold = 2, aligner = "ALIGN_PERCENTILE_99" }
      p0_low  = { comparison = "COMPARISON_LE", threshold = 0.5, aligner = "ALIGN_PERCENTILE_05" }
    } }
    refusal_ratio = { services = "policy", bounds = [0.5, 1, 1.5, 2, 3], bars = { refusal = { comparison = "COMPARISON_GE", threshold = 2, aligner = "ALIGN_PERCENTILE_99" } } }
    judge_score   = { services = "agent", bounds = [1, 2, 3, 3.5, 4, 5], bars = { judge = { comparison = "COMPARISON_LT", threshold = 3.5, aligner = "ALIGN_PERCENTILE_05" } } }
  }
  quality_bars = merge([
    for field, q in local.quality_series : { for bar, b in q.bars : bar => merge(b, { field = field }) }
  ]...)
}

resource "google_logging_metric" "quality" {
  for_each    = local.quality_series
  project     = var.project
  name        = "${var.environment}-${replace(each.key, "_", "-")}"
  description = "${each.key} from the metrics_snapshot lines of ${each.value.services} (nw/metrics_export.py)"
  filter      = "((resource.type=\"cloud_run_revision\" AND resource.labels.service_name=~\"${local.services_regex}\") OR resource.type=\"aiplatform.googleapis.com/ReasoningEngine\") AND jsonPayload.msg=\"metrics_snapshot\" AND jsonPayload.${each.key}:*"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "1"
    labels {
      key         = "service"
      value_type  = "STRING"
      description = "The emitting service (triage, semantic, policy, agent)"
    }
    labels {
      key         = "tenant"
      value_type  = "STRING"
      description = "NW_TENANT of the emitting service"
    }
  }
  value_extractor = "EXTRACT(jsonPayload.${each.key})"
  label_extractors = {
    service = "EXTRACT(jsonPayload.service)"
    tenant  = "EXTRACT(jsonPayload.tenant)"
  }
  bucket_options {
    explicit_buckets {
      bounds = each.value.bounds
    }
  }
}

resource "google_monitoring_alert_policy" "quality" {
  for_each     = local.quality_bars
  project      = var.project
  display_name = "${var.environment}: quality ${replace(each.key, "_", " ")} past its bar"
  combiner     = "OR"
  severity     = "WARNING"
  conditions {
    display_name = "${each.value.field} ${each.value.comparison} ${each.value.threshold}"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.quality[each.value.field].name}\""
      comparison      = each.value.comparison
      threshold_value = each.value.threshold
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = each.value.aligner
        cross_series_reducer = "REDUCE_NONE"
      }
    }
  }
  notification_channels = local.channels
  documentation {
    content = "A quality canary signal (${each.value.field}, deploy/SLO.md) is past its bar for ten minutes on the service and tenant the labels name. During a canary do not advance (`make approve-gcp` waits); roll back with `gcloud deploy targets rollback`."
  }
}

resource "google_logging_metric" "monitoring_anomalies" {
  project     = var.project
  name        = "${var.environment}-model-monitoring-anomalies"
  description = "Model Monitoring anomaly entries for the live endpoints"
  filter      = "log_id(\"aiplatform.googleapis.com/model_monitoring_anomalies\")"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "pipeline_errors" {
  project     = var.project
  name        = "${var.environment}-pipeline-errors"
  description = "Error entries from Vertex AI Pipelines runs"
  filter      = "resource.type=\"aiplatform.googleapis.com/PipelineJob\" AND severity>=ERROR"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "gateway_budget" {
  project     = var.project
  name        = "${var.environment}-gateway-budget"
  description = "Requests the gateway refused because a tenant key exceeded its budget"
  filter      = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${var.gateway_service}\" AND textPayload=~\"(?i)budget\" AND textPayload=~\"(?i)exceeded\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

# ----- alert policies -------------------------------------------------------------------------

locals {
  log_alerts = {
    drift = {
      metric  = google_logging_metric.drift_alerts.name
      title   = "drift alert logged"
      content = "A service or agent passed its drift bar (input and prediction PSI, retrieval confidence and refusal rate, agent cap and error rates). The tenant label says whose."
    }
    quality = {
      metric  = google_logging_metric.quality_alerts.name
      title   = "online quality below its bar"
      content = "A quality signal (the signal label names it) crossed its bar on a service or agent. During a canary this is a reason not to advance: `make approve-gcp` should wait, and the rollback is `gcloud deploy targets rollback`."
    }
    anomalies = {
      metric  = google_logging_metric.monitoring_anomalies.name
      title   = "Model Monitoring anomaly on a live endpoint"
      content = "Model Monitoring found skew or drift on a live endpoint. Open the monitor in the Agent Platform console and compare with the training profile."
    }
    pipelines = {
      metric  = google_logging_metric.pipeline_errors.name
      title   = "pipeline error"
      content = "A Vertex AI Pipelines run logged an error. `make runs-gcp` lists the last runs; the failed step's logs are in the run."
    }
    budget = {
      metric  = google_logging_metric.gateway_budget.name
      title   = "tenant budget exceeded on the gateway"
      content = "A tenant key hit its budget. Raise it with scripts/gcp_gateway_keys.sh or wait for the 30 day reset."
    }
  }
}

resource "google_monitoring_alert_policy" "log_based" {
  for_each     = local.log_alerts
  project      = var.project
  display_name = "${var.environment}: ${each.value.title}"
  combiner     = "OR"
  severity     = "WARNING"
  conditions {
    display_name = each.value.title
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${each.value.metric}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = local.channels
  documentation {
    content = each.value.content
  }
}

resource "google_monitoring_alert_policy" "gateway_errors" {
  project      = var.project
  display_name = "${var.environment}: gateway server errors"
  combiner     = "OR"
  severity     = "ERROR"
  conditions {
    display_name = "5xx responses above 5 in 5 minutes"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${var.gateway_service}\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = local.channels
  documentation {
    content = "The gateway is failing. Check its logs for provider errors and the Cloud SQL instance if virtual keys stopped resolving."
  }
}

resource "google_monitoring_alert_policy" "service_errors" {
  project      = var.project
  display_name = "${var.environment}: tenant service server errors"
  combiner     = "OR"
  severity     = "ERROR"
  conditions {
    display_name = "5xx responses above 5 in 5 minutes on any tenant service"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = monitoring.regex.full_match(\"${local.services_regex}\") AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.service_name"]
      }
    }
  }
  notification_channels = local.channels
}

# ----- dashboard ---------------------------------------------------------------------------------

locals {
  run_filter      = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = monitoring.regex.full_match(\"${local.services_regex}\")"
  gateway_filter  = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${var.gateway_service}\""
  endpoint_filter = "resource.type = \"aiplatform.googleapis.com/Endpoint\""
  agent_filter    = "resource.type = \"aiplatform.googleapis.com/ReasoningEngine\""
  chart = { for k, v in {
    services_requests = { title = "Serving: tenant service requests by service and class", filter = "${local.run_filter} AND metric.type = \"run.googleapis.com/request_count\"", aligner = "ALIGN_RATE", reducer = "REDUCE_SUM", group = ["resource.labels.service_name", "metric.labels.response_code_class"] }
    services_latency  = { title = "Serving: tenant service p95 latency by service", filter = "${local.run_filter} AND metric.type = \"run.googleapis.com/request_latencies\"", aligner = "ALIGN_PERCENTILE_95", reducer = "REDUCE_MAX", group = ["resource.labels.service_name"] }
    gateway_requests  = { title = "Gateway: requests by class", filter = "${local.gateway_filter} AND metric.type = \"run.googleapis.com/request_count\"", aligner = "ALIGN_RATE", reducer = "REDUCE_SUM", group = ["metric.labels.response_code_class"] }
    gateway_latency   = { title = "Gateway: p50 and p95 latency", filter = "${local.gateway_filter} AND metric.type = \"run.googleapis.com/request_latencies\"", aligner = "ALIGN_PERCENTILE_95", reducer = "REDUCE_MAX", group = [] }
    endpoint_requests = { title = "Live: endpoint predictions by endpoint and model", filter = "${local.endpoint_filter} AND metric.type = \"aiplatform.googleapis.com/prediction/online/prediction_count\"", aligner = "ALIGN_RATE", reducer = "REDUCE_SUM", group = ["resource.labels.endpoint_id", "metric.labels.deployed_model_id"] }
    endpoint_errors   = { title = "Live: endpoint error count", filter = "${local.endpoint_filter} AND metric.type = \"aiplatform.googleapis.com/prediction/online/error_count\"", aligner = "ALIGN_RATE", reducer = "REDUCE_SUM", group = ["resource.labels.endpoint_id"] }
    agent_requests    = { title = "Agents: Agent Engine requests by engine", filter = "${local.agent_filter} AND metric.type = \"aiplatform.googleapis.com/reasoning_engine/request_count\"", aligner = "ALIGN_RATE", reducer = "REDUCE_SUM", group = ["resource.labels.reasoning_engine_id"] }
    agent_latency     = { title = "Agents: Agent Engine p95 latency", filter = "${local.agent_filter} AND metric.type = \"aiplatform.googleapis.com/reasoning_engine/request_latencies\"", aligner = "ALIGN_PERCENTILE_95", reducer = "REDUCE_MAX", group = ["resource.labels.reasoning_engine_id"] }
    drift             = { title = "Operations: drift alerts by tenant", filter = "metric.type = \"logging.googleapis.com/user/${google_logging_metric.drift_alerts.name}\"", aligner = "ALIGN_SUM", reducer = "REDUCE_SUM", group = ["metric.labels.tenant"] }
    pipelines         = { title = "Operations: pipeline errors and monitoring anomalies", filter = "metric.type = \"logging.googleapis.com/user/${google_logging_metric.pipeline_errors.name}\"", aligner = "ALIGN_SUM", reducer = "REDUCE_SUM", group = [] }
    } : k => merge(v, { widget = {
      title = v.title
      xyChart = { dataSets = [{ timeSeriesQuery = { timeSeriesFilter = {
        filter      = v.filter
        aggregation = { alignmentPeriod = "60s", perSeriesAligner = v.aligner, crossSeriesReducer = v.reducer, groupByFields = v.group }
      } } }] }
    } })
  }
  order = ["services_requests", "services_latency", "gateway_requests", "gateway_latency", "endpoint_requests", "endpoint_errors", "agent_requests", "agent_latency", "drift", "pipelines"]
}

resource "google_monitoring_dashboard" "platform" {
  project = var.project
  dashboard_json = jsonencode({
    displayName = "${var.environment} platform"
    mosaicLayout = {
      columns = 12
      tiles = concat([
        for i, k in local.order : {
          xPos   = (i % 2) * 6
          yPos   = floor(i / 2) * 4
          width  = 6
          height = 4
          widget = local.chart[k].widget
        }
        ], [{
          xPos   = 0
          yPos   = 20
          width  = 12
          height = 2
          widget = {
            title = "Cost per tenant"
            text = {
              content = "Model spend per tenant is on the gateway: `curl -H 'Authorization: Bearer <master key>' <gateway>/spend/keys`. Cloud cost per tenant: billing export filtered on the `tenant` label. Budget alerts at 50, 80 and 100 percent of ${var.budget_usd} USD."
              format  = "MARKDOWN"
            }
          }
      }])
    }
  })
}

# ----- budget ---------------------------------------------------------------------------------

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "${var.environment}-monthly"
  budget_filter {
    projects = ["projects/${var.project}"]
  }
  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(floor(var.budget_usd))
    }
  }
  threshold_rules { threshold_percent = 0.5 }
  threshold_rules { threshold_percent = 0.8 }
  threshold_rules { threshold_percent = 1.0 }
  all_updates_rule {
    monitoring_notification_channels = local.channels
    disable_default_iam_recipients   = var.alert_email == "" ? false : true
  }
}

output "dashboard" { value = google_monitoring_dashboard.platform.id }
output "organization_policies" { value = local.organization_policies }
output "metrics" {
  value = {
    drift     = google_logging_metric.drift_alerts.name
    quality   = google_logging_metric.quality_alerts.name
    signals   = { for k, m in google_logging_metric.quality : k => m.name }
    anomalies = google_logging_metric.monitoring_anomalies.name
    pipelines = google_logging_metric.pipeline_errors.name
    budget    = google_logging_metric.gateway_budget.name
  }
}
