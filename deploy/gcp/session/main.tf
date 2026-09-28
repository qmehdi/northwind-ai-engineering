# The Session path on GCP: four Cloud Run services from the course images, least
# privilege service accounts, Cloud Monitoring dashboard and alerts, a billing budget.
#
#   make deploy-gcp TIER=session

locals {
  registry = "${var.region}-docker.pkg.dev/${var.project}/northwind"
  # The agent runs a multi-step resolution, so it gets 300 s where the tools get 120 s.
  services = {
    triage   = { app = "nw.triage.service:app", cpu = "1", memory = "2Gi", models = false, timeout = 120, env = {} }
    semantic = { app = "nw.semantic.service:app", cpu = "1", memory = "3Gi", models = false, timeout = 120, env = {} }
    policy   = { app = "nw.policy.service:app", cpu = "1", memory = "3Gi", models = true, timeout = 120, env = {} }
    agent    = { app = "nw.agent.service:app", cpu = "2", memory = "4Gi", models = true, timeout = 300, env = { NW_AGENT_ROLE = "resolver", NW_SPEND_CAP_USD = "25" } }
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
resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "google_secret_manager_secret" "api_key" {
  project   = var.project
  secret_id = "northwind-api-key"
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
  depends_on     = [google_project_service.apis, data.google_artifact_registry_repository.images, google_secret_manager_secret_version.api_key]
}

# ----- observability ------------------------------------------------------------

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  project      = var.project
  display_name = "Northwind alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
  depends_on   = [google_project_service.apis]
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
  depends_on = [google_project_service.apis]
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
  depends_on            = [google_project_service.apis]
}

resource "google_monitoring_dashboard" "northwind" {
  project    = var.project
  depends_on = [google_project_service.apis]
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
  name    = "northwind-drift-alerts"
  filter  = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=~\"^northwind-\" AND jsonPayload.msg=\"drift_alert\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "triage_drift" {
  project      = var.project
  display_name = "northwind drift"
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
