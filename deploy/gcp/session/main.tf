# The Session path on GCP: four Cloud Run services from the course images, least
# privilege service accounts, Cloud Monitoring dashboard and alerts, a billing budget.
#
#   make deploy-gcp TIER=session

locals {
  registry = "${var.region}-docker.pkg.dev/${var.project}/northwind"
  services = {
    triage   = { app = "nw.triage.service:app", cpu = "1", memory = "2Gi", models = false, env = {} }
    semantic = { app = "nw.semantic.service:app", cpu = "1", memory = "3Gi", models = false, env = {} }
    policy   = { app = "nw.policy.service:app", cpu = "1", memory = "3Gi", models = true, env = {} }
    agent    = { app = "nw.agent.service:app", cpu = "2", memory = "4Gi", models = true, env = { NW_AGENT_ROLE = "resolver" } }
  }
}

resource "google_project_service" "apis" {
  for_each = toset(["run.googleapis.com", "artifactregistry.googleapis.com", "aiplatform.googleapis.com", "monitoring.googleapis.com", "cloudtrace.googleapis.com", "logging.googleapis.com", "billingbudgets.googleapis.com"])
  project  = var.project
  service  = each.value

  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  project       = var.project
  location      = var.region
  repository_id = "northwind"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

module "service" {
  for_each      = local.services
  source        = "../modules/service"
  name          = each.key
  project       = var.project
  region        = var.region
  image         = "${local.registry}/nw-${each.key}:${var.image_tag}"
  cpu           = each.value.cpu
  memory        = each.value.memory
  invoke_models = each.value.models
  env           = each.value.env
  public        = each.key == "agent"
  depends_on    = [google_artifact_registry_repository.images]
}

# ----- observability ------------------------------------------------------------

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  project      = var.project
  display_name = "Northwind alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

resource "google_monitoring_alert_policy" "errors" {
  for_each     = local.services
  project      = var.project
  display_name = "northwind-${each.key}: server errors"
  combiner     = "OR"
  severity     = "ERROR"
  conditions {
    display_name = "5xx responses above 5 in 5 minutes"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"northwind-${each.key}\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
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
    content = "More than five 5xx responses in five minutes. Check the revision's logs and /readyz."
  }
}

resource "google_monitoring_alert_policy" "latency" {
  for_each     = local.services
  project      = var.project
  display_name = "northwind-${each.key}: p95 latency"
  combiner     = "OR"
  severity     = "WARNING"
  conditions {
    display_name = "p95 request latency above 8 seconds"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"northwind-${each.key}\" AND metric.type = \"run.googleapis.com/request_latencies\""
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
}

resource "google_monitoring_dashboard" "northwind" {
  project = var.project
  dashboard_json = jsonencode({
    displayName = "northwind"
    mosaicLayout = {
      columns = 12
      tiles = flatten([
        for i, name in keys(local.services) : [
          {
            xPos = 0, yPos = i * 4, width = 6, height = 4
            widget = {
              title = "northwind-${name} requests by class"
              xyChart = { dataSets = [{ timeSeriesQuery = { timeSeriesFilter = {
                filter      = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"northwind-${name}\" AND metric.type = \"run.googleapis.com/request_count\""
                aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["metric.labels.response_code_class"] }
              } } }] }
            }
          },
          {
            xPos = 6, yPos = i * 4, width = 6, height = 4
            widget = {
              title = "northwind-${name} latency p50 and p95"
              xyChart = { dataSets = [
                { timeSeriesQuery = { timeSeriesFilter = { filter = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"northwind-${name}\" AND metric.type = \"run.googleapis.com/request_latencies\"", aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_50" } } } },
                { timeSeriesQuery = { timeSeriesFilter = { filter = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"northwind-${name}\" AND metric.type = \"run.googleapis.com/request_latencies\"", aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_PERCENTILE_95" } } } }
              ] }
            }
          }
        ]
      ])
    }
  })
}

# ----- budget -------------------------------------------------------------------

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "northwind-monthly"
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
