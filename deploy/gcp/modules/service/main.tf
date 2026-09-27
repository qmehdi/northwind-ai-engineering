# One Cloud Run v2 service from a course image, with a dedicated service account,
# readiness on /readyz, request-based billing, and no public invoker unless asked.
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

resource "google_service_account" "svc" {
  project      = var.project
  account_id   = "northwind-${var.name}"
  display_name = "Northwind ${var.name} service"
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

resource "google_cloud_run_v2_service" "svc" {
  depends_on          = [google_secret_manager_secret_iam_member.api_key]
  project             = var.project
  name                = "northwind-${var.name}"
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
        for_each = merge({ NW_TRACK = "gcp", NW_GCP_PROJECT = var.project, NW_GCP_REGION = "global", NW_LOG_FORMAT = "json", PORT = "8000", NW_TRACE_EXPORT = "cloudtrace", OTEL_SERVICE_NAME = "northwind-${var.name}" }, var.env)
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
