# One Cloud Run v2 service from a course image, with a dedicated service account,
# readiness on /readyz, request-based billing, and no public invoker unless asked.
#
# Traffic: with canary_percent 0 (the default) the newest ready revision takes all traffic
# under the tag `latest`. With canary_percent N the newest revision takes N percent under
# `latest` and the revision that was serving before this apply keeps the rest under
# `stable`; both tags get their own URL (https://latest---<service url>) so either can be
# probed directly. Promote with canary_percent 0 again, or roll back with
# `gcloud run services update-traffic <service> --to-tags stable=100`.
variable "name" { type = string }
variable "project" { type = string }
variable "region" { type = string }
variable "image" { type = string }
variable "env" {
  type    = map(string)
  default = {}
}
variable "cpu" {
  type    = string
  default = "1"
}
variable "memory" {
  type    = string
  default = "2Gi"
}
variable "min_instances" {
  type    = number
  default = 0
}
variable "max_instances" {
  type    = number
  default = 3
}
variable "public" {
  type    = bool
  default = false
}
variable "timeout" {
  type        = number
  default     = 120
  description = "Request timeout in seconds; the agent needs 300 for a multi-step resolution"
}
variable "invoke_models" {
  type        = bool
  default     = false
  description = "Grant roles/aiplatform.user so the service can call Claude on Vertex AI"
}
variable "api_key_secret" {
  type        = string
  default     = ""
  description = "Secret Manager secret id holding the service API key; injected as NW_API_KEY at runtime"
}
variable "stage" {
  type        = string
  default     = ""
  description = "Put after `northwind` in every name so dev, staging and prod can share a project; empty keeps the guide's names"
  validation {
    condition     = can(regex("^[a-z0-9]{0,7}$", var.stage))
    error_message = "stage is lowercase letters and digits, at most 7 characters, so northwind-<stage>-agent-engine fits a 30 character service account id."
  }
}
variable "canary_percent" {
  type        = number
  default     = 0
  description = "Traffic share for the newest revision; 0 sends everything to it, N keeps 100 minus N on the previously serving revision under the tag `stable`"
  validation {
    condition     = var.canary_percent >= 0 && var.canary_percent <= 100
    error_message = "canary_percent is between 0 and 100."
  }
}

locals {
  prefix  = var.stage == "" ? "northwind" : "northwind-${var.stage}"
  service = "${local.prefix}-${var.name}"
}

resource "google_service_account" "svc" {
  project      = var.project
  account_id   = local.service
  display_name = "Northwind ${var.name} service${var.stage == "" ? "" : " (${var.stage})"}"
}

# Least privilege: only services that call a model get aiplatform.user, and nothing gets
# a project-wide editor role. Logs and traces are written through the default agents.
resource "google_project_iam_member" "aiplatform" {
  count   = var.invoke_models ? 1 : 0
  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.svc.email}"
}

resource "google_project_iam_member" "trace" {
  project = var.project
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.svc.email}"
}

resource "google_project_iam_member" "metrics" {
  project = var.project
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.svc.email}"
}

# The API key is read by Cloud Run from Secret Manager at revision start and handed to the
# container as an environment variable. Only this service account may read this one secret.
resource "google_secret_manager_secret_iam_member" "api_key" {
  count     = var.api_key_secret == "" ? 0 : 1
  project   = var.project
  secret_id = var.api_key_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc.email}"
}

# The revision serving before this apply: the `stable` side of a canary. Read only when a
# canary is asked for, so the first apply (no service yet) needs canary_percent 0.
data "google_cloud_run_v2_service" "current" {
  count    = var.canary_percent > 0 ? 1 : 0
  project  = var.project
  location = var.region
  name     = local.service
}

resource "google_cloud_run_v2_service" "svc" {
  depends_on          = [google_secret_manager_secret_iam_member.api_key]
  project             = var.project
  name                = local.service
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.svc.email
    timeout                          = "${var.timeout}s"
    max_instance_request_concurrency = 20

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      ports {
        container_port = 8000
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle          = true # request-based billing: CPU only while serving
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = merge({
          NW_TRACK          = "gcp",
          NW_GCP_PROJECT    = var.project,
          NW_GCP_REGION     = "global",
          NW_LOG_FORMAT     = "json",
          PORT              = "8000",
          NW_TRACE_EXPORT   = "cloudtrace",
          OTEL_SERVICE_NAME = local.service,
          # One `metrics_snapshot` JSON line per minute of activity; the log-based metrics in
          # the session tier turn its fields into Cloud Monitoring series.
          NW_METRICS_FORMAT = "json",
          NW_STAGE          = var.stage,
        }, var.env)
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.api_key_secret == "" ? [] : [var.api_key_secret]
        content {
          name = "NW_API_KEY"
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        initial_delay_seconds = 10
        period_seconds        = 10
        failure_threshold     = 12
        timeout_seconds       = 5
        http_get {
          path = "/readyz"
          port = 8000
        }
      }

      liveness_probe {
        period_seconds    = 30
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8000
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = var.canary_percent == 0 ? 100 : var.canary_percent
    tag     = "latest"
  }

  dynamic "traffic" {
    for_each = var.canary_percent == 0 ? [] : [1]
    content {
      type     = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
      revision = data.google_cloud_run_v2_service.current[0].latest_ready_revision
      percent  = 100 - var.canary_percent
      tag      = "stable"
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.public ? 1 : 0
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.svc.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "url" { value = google_cloud_run_v2_service.svc.uri }
output "service_account" { value = google_service_account.svc.email }
output "name" { value = google_cloud_run_v2_service.svc.name }
output "latest_revision" { value = google_cloud_run_v2_service.svc.latest_ready_revision }
