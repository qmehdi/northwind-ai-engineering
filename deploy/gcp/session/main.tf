# The Session path on GCP: four Cloud Run services from the course images, least
# privilege service accounts, Cloud Monitoring dashboard and alerts, a billing budget.
#
#   make deploy-gcp TIER=session
#   make deploy-gcp TIER=session CANARY=10      # newest revision on 10 percent, `stable` keeps 90
#   NW_STAGE=staging make deploy-gcp TIER=session  # northwind-staging-* names in the same project

locals {
  registry = "${var.region}-docker.pkg.dev/${var.project}/northwind"
  prefix   = var.stage == "" ? "northwind" : "northwind-${var.stage}"
  # The agent runs a multi-step resolution, so it gets 300 s where the tools get 120 s.
  # p95_ms is the service's own latency bar (deploy/SLO.md): the last finite bucket of the
  # triage and semantic histograms, the 8 second alarm the policy service already carries,
  # and the cost sheet's 30 seconds per agent run.
  services = {
    triage   = { app = "nw.triage.service:app", cpu = "1", memory = "2Gi", models = false, timeout = 120, p95_ms = 2000, env = {} }
    semantic = { app = "nw.semantic.service:app", cpu = "1", memory = "3Gi", models = false, timeout = 120, p95_ms = 2000, env = {} }
    policy   = { app = "nw.policy.service:app", cpu = "1", memory = "3Gi", models = true, timeout = 120, p95_ms = 8000, env = {} }
    agent    = { app = "nw.agent.service:app", cpu = "2", memory = "4Gi", models = true, timeout = 300, p95_ms = 30000, env = { NW_AGENT_ROLE = "resolver", NW_SPEND_CAP_USD = "25" } }
  }
  # The fields nw/metrics_export.py writes in its `metrics_snapshot` line, one log-based
  # metric per service and field. Counters arrive as per-interval deltas, so DELTA sums are
  # counts; the p95 is a distribution so a percentile aligner reads it rather than summing.
  exported = {
    requests = { field = "requests", kind = "INT64", unit = "1" }
    errors   = { field = "errors", kind = "INT64", unit = "1" }
    p95      = { field = "latency_p95_ms", kind = "DISTRIBUTION", unit = "ms" }
    cost     = { field = "cost_usd", kind = "DOUBLE", unit = "1" }
    drift    = { field = "drift_level", kind = "INT64", unit = "1" }
  }
  exported_series = {
    for pair in setproduct(keys(local.services), keys(local.exported)) :
    "${pair[0]}-${pair[1]}" => merge({ service = pair[0], name = pair[1] }, local.exported[pair[1]])
  }
}

resource "google_project_service" "apis" {
  # iam and cloudresourcemanager back the service accounts and project IAM bindings the
  # service module creates; on a fresh project they are not on by default.
  for_each = toset(["run.googleapis.com", "artifactregistry.googleapis.com", "aiplatform.googleapis.com", "monitoring.googleapis.com", "cloudtrace.googleapis.com", "logging.googleapis.com", "billingbudgets.googleapis.com", "secretmanager.googleapis.com", "iam.googleapis.com", "cloudresourcemanager.googleapis.com"])
  project  = var.project
  service  = each.value

  disable_on_destroy = false
}

# One API key for the cohort's services, generated here and never written to a file or an
# image. `terraform output -raw api_key` prints it once for the curl commands in the guide.
# `scripts/rotate_key.sh` adds a new version out of band; after that this output is stale
# and the script's printed key is the live one.
resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "google_secret_manager_secret" "api_key" {
  project   = var.project
  secret_id = "${local.prefix}-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "api_key" {
  secret      = google_secret_manager_secret.api_key.id
  secret_data = random_password.api_key.result
}

# The repository is created by scripts/images_gcp.sh before the images are pushed, because
# the push has to succeed before this apply can create services from those images. Terraform
# only checks it exists; `make destroy-gcp TIER=session` deletes it with gcloud afterwards.
# Stages share it: the image tag (the git SHA) names the code, the stage names the deployment.
data "google_artifact_registry_repository" "images" {
  project       = var.project
  location      = var.region
  repository_id = "northwind"
  depends_on    = [google_project_service.apis]
}

module "service" {
  for_each       = local.services
  source         = "../modules/service"
  name           = each.key
  project        = var.project
  region         = var.region
  image          = "${local.registry}/nw-${each.key}:${var.image_tag}"
  cpu            = each.value.cpu
  memory         = each.value.memory
  invoke_models  = each.value.models
  timeout        = each.value.timeout
  env            = each.value.env
  public         = true
  api_key_secret = google_secret_manager_secret.api_key.secret_id
  stage          = var.stage
  canary_percent = var.canary_percent
  depends_on     = [google_project_service.apis, data.google_artifact_registry_repository.images, google_secret_manager_secret_version.api_key]
}

# ----- observability ------------------------------------------------------------

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  project      = var.project
  display_name = "${local.prefix} alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
  depends_on   = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "errors" {
  for_each     = local.services
  project      = var.project
  display_name = "${local.prefix}-${each.key}: server errors"
  combiner     = "OR"
  severity     = "ERROR"
  conditions {
    display_name = "5xx responses above 5 in 5 minutes"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${local.prefix}-${each.key}\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = [for c in google_monitoring_notification_channel.email : c.id]
  documentation {
    content = "More than five 5xx responses in five minutes. Check the revision's logs and /readyz. During a canary, the request panel split by revision says which side."
  }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "latency" {
  for_each     = local.services
  project      = var.project
  display_name = "${local.prefix}-${each.key}: p95 latency"
  combiner     = "OR"
  severity     = "WARNING"
  conditions {
    display_name = "p95 request latency above 8 seconds"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${local.prefix}-${each.key}\" AND metric.type = \"run.googleapis.com/request_latencies\""
      comparison      = "COMPARISON_GT"
      threshold_value = 8000
      duration        = "900s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }
  notification_channels = [for c in google_monitoring_notification_channel.email : c.id]
  depends_on            = [google_project_service.apis]
}

# ----- the services' own metrics, through the log line ---------------------------
#
# Nothing scrapes a Cloud Run instance, so each service writes a `metrics_snapshot` JSON
# line every minute of activity (nw/metrics_export.py). A log-based metric per service and
# field extracts the number; the dashboard and the per-service p95 alert read those.

resource "google_logging_metric" "exported" {
  for_each        = local.exported_series
  project         = var.project
  name            = "${local.prefix}-${each.value.service}-${each.value.name}"
  description     = "${each.value.field} from ${local.prefix}-${each.value.service}'s metrics_snapshot line"
  filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${local.prefix}-${each.value.service}\" AND jsonPayload.msg=\"metrics_snapshot\" AND jsonPayload.${each.value.field}:*"
  value_extractor = "EXTRACT(jsonPayload.${each.value.field})"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = each.value.kind
    unit        = each.value.unit
  }
  dynamic "bucket_options" {
    for_each = each.value.kind == "DISTRIBUTION" ? [1] : []
    content {
      explicit_buckets {
        bounds = [100, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000, 64000]
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "exported_latency" {
  for_each     = local.services
  project      = var.project
  display_name = "${local.prefix}-${each.key}: p95 latency, service-measured"
  combiner     = "OR"
  severity     = "WARNING"
  conditions {
    display_name = "exported p95 above ${each.value.p95_ms} ms for 15 minutes"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.exported["${each.key}-p95"].name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.p95_ms
      duration        = "900s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
        group_by_fields      = ["resource.labels.service_name"]
      }
    }
  }
  notification_channels = [for c in google_monitoring_notification_channel.email : c.id]
  documentation {
    content = "The service's own p95 (from its latency histogram, exported every minute) is above the bar in deploy/SLO.md. Cloud Run's request_latencies alert measures the same path from outside; when only this one fires the time is inside the process."
  }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_dashboard" "northwind" {
  project    = var.project
  depends_on = [google_project_service.apis]
  dashboard_json = jsonencode({
    displayName = local.prefix
    mosaicLayout = {
      columns = 12
      tiles = flatten([[
        # Rows 0 to 15: what Cloud Run saw, per service, split by revision so a canary reads
        # as two lines on each panel.
        for i, name in keys(local.services) : [
          {
            xPos = 0, yPos = i * 4, width = 6, height = 4
            widget = {
              title = "${local.prefix}-${name} requests by class and revision"
              xyChart = { dataSets = [{ timeSeriesQuery = { timeSeriesFilter = {
                filter      = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${local.prefix}-${name}\" AND metric.type = \"run.googleapis.com/request_count\""
                aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["metric.labels.response_code_class", "resource.labels.revision_name"] }
              } } }] }
            }
          },
          {
            xPos = 6, yPos = i * 4, width = 6, height = 4
            widget = {
              title = "${local.prefix}-${name} latency p50 and p95 by revision"
              xyChart = { dataSets = [
                { timeSeriesQuery = { timeSeriesFilter = { filter = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${local.prefix}-${name}\" AND metric.type = \"run.googleapis.com/request_latencies\"", aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_50", crossSeriesReducer = "REDUCE_MAX", groupByFields = ["resource.labels.revision_name"] } } } },
                { timeSeriesQuery = { timeSeriesFilter = { filter = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${local.prefix}-${name}\" AND metric.type = \"run.googleapis.com/request_latencies\"", aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_95", crossSeriesReducer = "REDUCE_MAX", groupByFields = ["resource.labels.revision_name"] } } } }
              ] }
            }
          },
          # Rows 16 to 31: what the service measured itself, from the exported line.
          {
            xPos = 0, yPos = 16 + i * 4, width = 6, height = 4
            widget = {
              title = "${local.prefix}-${name} requests and errors (exported)"
              xyChart = { dataSets = [
                { timeSeriesQuery = { timeSeriesFilter = { filter = "metric.type = \"logging.googleapis.com/user/${local.prefix}-${name}-requests\" AND resource.type = \"cloud_run_revision\"", aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["resource.labels.service_name"] } } } },
                { timeSeriesQuery = { timeSeriesFilter = { filter = "metric.type = \"logging.googleapis.com/user/${local.prefix}-${name}-errors\" AND resource.type = \"cloud_run_revision\"", aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["resource.labels.service_name"] } } } }
              ] }
            }
          },
          {
            xPos = 6, yPos = 16 + i * 4, width = 6, height = 4
            widget = {
              title = "${local.prefix}-${name} p95 latency ms (exported)"
              xyChart = { dataSets = [{ timeSeriesQuery = { timeSeriesFilter = {
                filter      = "metric.type = \"logging.googleapis.com/user/${local.prefix}-${name}-p95\" AND resource.type = \"cloud_run_revision\""
                aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_95", crossSeriesReducer = "REDUCE_MAX", groupByFields = ["resource.labels.service_name"] }
              } } }] }
            }
          }
        ]
        ], [
        # Row 32: the three signals AI systems degrade on, across services.
        {
          xPos = 0, yPos = 32, width = 6, height = 4
          widget = {
            title = "model cost per minute, USD (exported)"
            xyChart = { dataSets = [for name in ["policy", "agent"] :
              { timeSeriesQuery = { timeSeriesFilter = { filter = "metric.type = \"logging.googleapis.com/user/${local.prefix}-${name}-cost\" AND resource.type = \"cloud_run_revision\"", aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["resource.labels.service_name"] } } } }
            ] }
          }
        },
        {
          xPos = 6, yPos = 32, width = 6, height = 4
          widget = {
            title = "drift level per service (exported: 0 ok, 1 watch, 2 alert)"
            xyChart = { dataSets = [for name in keys(local.services) :
              { timeSeriesQuery = { timeSeriesFilter = { filter = "metric.type = \"logging.googleapis.com/user/${local.prefix}-${name}-drift\" AND resource.type = \"cloud_run_revision\"", aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_MAX", crossSeriesReducer = "REDUCE_MAX", groupByFields = ["resource.labels.service_name"] } } } }
            ] }
          }
        }
      ]])
    }
  })
}

# ----- budget -------------------------------------------------------------------

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "${local.prefix}-monthly"
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
    monitoring_notification_channels = [for c in google_monitoring_notification_channel.email : c.id]
    disable_default_iam_recipients   = var.alert_email == "" ? false : true
  }
  depends_on = [google_project_service.apis]
}

output "urls" { value = { for k, m in module.service : k => m.url } }
output "revisions" { value = { for k, m in module.service : k => m.latest_revision } }
output "stage" { value = var.stage == "" ? "default" : var.stage }
output "api_key" {
  value     = random_password.api_key.result
  sensitive = true
}

# Every service logs `drift_alert` when its drift signal passes the bar (input and
# prediction PSI, retrieval confidence and refusal rate, agent cap and error rates); a
# log-based metric counts those lines across the northwind services and the alert policy
# emails on the first one, with the service name as a label.
resource "google_logging_metric" "triage_drift" {
  project = var.project
  name    = "${local.prefix}-drift-alerts"
  filter  = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=~\"^${local.prefix}-(triage|semantic|policy|agent)$\" AND jsonPayload.msg=\"drift_alert\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "triage_drift" {
  project      = var.project
  display_name = "${local.prefix} drift"
  combiner     = "OR"
  conditions {
    display_name = "drift alert logged"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.triage_drift.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = [for c in google_monitoring_notification_channel.email : c.id]
  depends_on            = [google_project_service.apis]
}
