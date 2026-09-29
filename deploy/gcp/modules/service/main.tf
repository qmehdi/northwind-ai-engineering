# One Cloud Run v2 service from a course image with its own service account, readiness on
# /readyz, request-based billing, and no public invoker unless asked. Used for every tenant
# service and for the gateway-facing pieces of the platform; the live services are created by
# Cloud Deploy from deploy/gcp/platform/delivery, not by this module.
#
# Traffic: with canary_percent 0 the newest ready revision takes all traffic under the tag
# `latest`. With N the newest revision takes N percent under `latest` and the revision that was
# serving before keeps the rest under `stable`. Promote with 0 again, or roll back with
# `gcloud run services update-traffic <service> --to-tags stable=100`.
variable "name" {
  type        = string
  description = "Full service name, already prefixed (northwind-alice-triage)"
}
variable "account_id" {
  type        = string
  description = "Service account id, at most 30 characters (nw-alice-triage)"
}
variable "project" { type = string }
variable "region" { type = string }
variable "image" { type = string }
variable "labels" {
  type    = map(string)
  default = {}
}
variable "env" {
  type    = map(string)
  default = {}
}
variable "secret_env" {
  type        = map(string)
  default     = {}
  description = "Environment variable name to Secret Manager secret id; the latest version is injected at instance start"
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
  type    = number
  default = 120
}
variable "invoke_models" {
  type        = bool
  default     = false
  description = "Grant roles/aiplatform.user so the service can call the Agent Platform directly (the gateway is the normal path)"
}
variable "buckets_read" {
  type        = list(string)
  default     = []
  description = "Buckets the service reads model artifacts from"
}
variable "canary_percent" {
  type    = number
  default = 0
  validation {
    condition     = var.canary_percent >= 0 && var.canary_percent <= 100
    error_message = "canary_percent is between 0 and 100."
  }
}

resource "google_service_account" "svc" {
  project      = var.project
  account_id   = var.account_id
  display_name = var.name
}

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

resource "google_project_iam_member" "logs" {
  project = var.project
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.svc.email}"
}

resource "google_storage_bucket_iam_member" "read" {
  for_each = toset(var.buckets_read)
  bucket   = each.value
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${google_service_account.svc.email}"
}

# Only this service account may read the secrets it is handed.
resource "google_secret_manager_secret_iam_member" "secrets" {
  for_each  = var.secret_env
  project   = var.project
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc.email}"
}

# The revision serving before this apply: the `stable` side of a canary. Read only when a
# canary is asked for, so the first apply (no service yet) needs canary_percent 0.
data "google_cloud_run_v2_service" "current" {
  count    = var.canary_percent > 0 ? 1 : 0
  project  = var.project
  location = var.region
  name     = var.name
}

resource "google_cloud_run_v2_service" "svc" {
  depends_on          = [google_secret_manager_secret_iam_member.secrets]
  project             = var.project
  name                = var.name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false
  labels              = var.labels

  template {
    service_account                  = google_service_account.svc.email
    timeout                          = "${var.timeout}s"
    max_instance_request_concurrency = 20
    labels                           = var.labels

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
          OTEL_SERVICE_NAME = var.name,
          NW_METRICS_FORMAT = "json",
        }, var.env)
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.secret_env
        content {
          name = env.key
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

  # After the first apply the platform client owns the container's environment: a registry
  # deploy rewrites NW_MODEL_URI on the tenant's service (nw/platform/gcp.py) and Terraform must
  # not put the old value back. To change an env value from Terraform, replace the service:
  #   terraform apply -replace='module.serving.module.service["alice-triage"].google_cloud_run_v2_service.svc'
  lifecycle {
    ignore_changes = [template[0].containers[0].env]
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
