# Delivery (ADR 0012): pull-request checks stay in GitHub Actions; the image is built once by
# Cloud Build on a push to main, pushed by digest to Artifact Registry, and promoted by Cloud
# Deploy into the live target with a canary and a manual approval. The Cloud Build triggers
# need the GitHub connection (github_owner set); the Cloud Deploy pipeline exists regardless
# so `make release-gcp` works from a laptop.
variable "project" { type = string }
variable "project_number" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "artifacts_bucket" { type = string }
variable "live_service_account" { type = string }
variable "github_owner" { type = string }
variable "github_repo" { type = string }
variable "github_branch" { type = string }
variable "github_app_installation_id" { type = number }
variable "github_oauth_token_secret_version" { type = string }
variable "canary_percent" { type = number }

locals {
  github                 = var.github_owner != ""
  cloudbuild_agent       = "service-${var.project_number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
  clouddeploy_agent      = "service-${var.project_number}@gcp-sa-clouddeploy.iam.gserviceaccount.com"
  github_token_secret    = local.github && var.github_oauth_token_secret_version != "" ? regex("^(projects/[^/]+/secrets/[^/]+)/versions/", var.github_oauth_token_secret_version)[0] : ""
  github_token_secret_id = local.github_token_secret == "" ? "" : element(split("/", local.github_token_secret), length(split("/", local.github_token_secret)) - 1)
}

# ----- Artifact Registry --------------------------------------------------------------------
# The course images (scripts/images_gcp.sh creates the repository when it runs before the
# first apply; scripts/deploy_gcp.sh imports it into state in that case) and a remote
# repository that proxies ghcr.io for the LiteLLM image, because Cloud Run pulls only from
# Artifact Registry and Container Registry.

resource "google_artifact_registry_repository" "images" {
  project       = var.project
  location      = var.region
  repository_id = var.environment
  format        = "DOCKER"
  description   = "Course images of ${var.environment}: nw-triage, nw-semantic, nw-policy, nw-agent, nw-mcp"
  labels        = merge(var.labels, { area = "delivery" })
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
}

resource "google_artifact_registry_repository" "remote" {
  project       = var.project
  location      = var.region
  repository_id = "${var.environment}-remote"
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"
  description   = "Proxy of ghcr.io for the LiteLLM gateway image"
  labels        = merge(var.labels, { area = "delivery" })
  remote_repository_config {
    description = "ghcr.io"
    docker_repository {
      custom_repository {
        uri = "https://ghcr.io"
      }
    }
  }
}

# ----- identities -----------------------------------------------------------------------------

resource "google_service_account" "builder" {
  project      = var.project
  account_id   = "${var.environment}-builder"
  display_name = "${var.environment} Cloud Build"
}

resource "google_service_account" "deployer" {
  project      = var.project
  account_id   = "${var.environment}-deployer"
  display_name = "${var.environment} Cloud Deploy execution"
}

resource "google_project_iam_member" "builder" {
  for_each = toset(["roles/logging.logWriter", "roles/artifactregistry.writer", "roles/clouddeploy.releaser"])
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_project_iam_member" "deployer" {
  for_each = toset(["roles/clouddeploy.jobRunner", "roles/run.developer", "roles/artifactregistry.reader", "roles/logging.logWriter"])
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_storage_bucket_iam_member" "builder_artifacts" {
  bucket = var.artifacts_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_storage_bucket_iam_member" "deployer_artifacts" {
  bucket = var.artifacts_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.deployer.email}"
}

# A release runs as the deployer, so the builder (and the Cloud Deploy service agent) must be
# allowed to act as it; the deployer in turn deploys services that run as the live identity.
resource "google_service_account_iam_member" "builder_acts_as_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_service_account_iam_member" "clouddeploy_acts_as_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.clouddeploy_agent}"
}

resource "google_service_account_iam_member" "deployer_acts_as_live" {
  service_account_id = "projects/${var.project}/serviceAccounts/${var.live_service_account}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# ----- Cloud Build on the GitHub repository ------------------------------------------------------

resource "google_secret_manager_secret_iam_member" "github_token" {
  count     = local.github_token_secret_id == "" ? 0 : 1
  project   = var.project
  secret_id = local.github_token_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.cloudbuild_agent}"
}

resource "google_cloudbuildv2_connection" "github" {
  count    = local.github ? 1 : 0
  project  = var.project
  location = var.region
  name     = "${var.environment}-github"
  github_config {
    app_installation_id = var.github_app_installation_id
    authorizer_credential {
      oauth_token_secret_version = var.github_oauth_token_secret_version
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.github_token]
}

resource "google_cloudbuildv2_repository" "course" {
  count             = local.github ? 1 : 0
  project           = var.project
  location          = var.region
  name              = "${var.environment}-${var.github_repo}"
  parent_connection = google_cloudbuildv2_connection.github[0].id
  remote_uri        = "https://github.com/${var.github_owner}/${var.github_repo}.git"
}

# Pull request: the same checks GitHub Actions runs, so a reviewer sees both greens; no image.
resource "google_cloudbuild_trigger" "pull_request" {
  count           = local.github ? 1 : 0
  project         = var.project
  location        = var.region
  name            = "${var.environment}-pull-request"
  description     = "Checks on every pull request against ${var.github_branch}"
  service_account = google_service_account.builder.id
  filename        = "deploy/gcp/platform/delivery/cloudbuild-pr.yaml"
  repository_event_config {
    repository = google_cloudbuildv2_repository.course[0].id
    pull_request {
      branch          = "^${var.github_branch}$"
      comment_control = "COMMENTS_ENABLED_FOR_EXTERNAL_CONTRIBUTORS_ONLY"
    }
  }
  substitutions = {
    _ENVIRONMENT = var.environment
  }
  tags = ["northwind", "pull-request"]
}

# Main: build every image once, push by digest, create the Cloud Deploy release. The
# rollout then waits for the approval in the console or `make approve-gcp`.
resource "google_cloudbuild_trigger" "main" {
  count           = local.github ? 1 : 0
  project         = var.project
  location        = var.region
  name            = "${var.environment}-main"
  description     = "Build, push by digest and release on every push to ${var.github_branch}"
  service_account = google_service_account.builder.id
  filename        = "deploy/gcp/platform/delivery/cloudbuild-main.yaml"
  repository_event_config {
    repository = google_cloudbuildv2_repository.course[0].id
    push {
      branch = "^${var.github_branch}$"
    }
  }
  substitutions = {
    _ENVIRONMENT = var.environment
    _REGION      = var.region
    _REGISTRY    = "${var.region}-docker.pkg.dev/${var.project}/${var.environment}"
    _PIPELINE    = "${var.environment}-live"
    _LIVE_SA     = var.live_service_account
    _DEPLOYER_SA = google_service_account.deployer.email
  }
  tags = ["northwind", "main"]
}

# ----- Cloud Deploy: one target, a canary, a manual approval ----------------------------------------
# A higher environment is a second target in this pipeline with its own execution service
# account in the other project (README, "Lower and higher environments"); the course deploys
# one.

resource "google_clouddeploy_target" "live" {
  project          = var.project
  location         = var.region
  name             = "${var.environment}-live"
  description      = "The live Cloud Run services of ${var.environment}"
  require_approval = true
  labels           = merge(var.labels, { area = "delivery" })
  run {
    location = "projects/${var.project}/locations/${var.region}"
  }
  execution_configs {
    usages            = ["RENDER", "DEPLOY"]
    service_account   = google_service_account.deployer.email
    artifact_storage  = "gs://${var.artifacts_bucket}/clouddeploy"
    execution_timeout = "3600s"
  }
  deploy_parameters = {
    environment = var.environment
  }
}

resource "google_clouddeploy_delivery_pipeline" "live" {
  project     = var.project
  location    = var.region
  name        = "${var.environment}-live"
  description = "Promotion of the course services into the live target of ${var.environment}"
  labels      = merge(var.labels, { area = "delivery" })
  serial_pipeline {
    stages {
      target_id = google_clouddeploy_target.live.name
      profiles  = ["live"]
      strategy {
        canary {
          runtime_config {
            cloud_run {
              automatic_traffic_control = true
              canary_revision_tags      = ["canary"]
              stable_revision_tags      = ["stable"]
            }
          }
          canary_deployment {
            percentages = [var.canary_percent]
            verify      = false
          }
        }
      }
    }
  }
}

output "images_repository" { value = google_artifact_registry_repository.images.name }
output "remote_registry" { value = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.remote.repository_id}" }
output "pipeline" { value = google_clouddeploy_delivery_pipeline.live.name }
output "target" { value = google_clouddeploy_target.live.name }
output "builder_service_account" { value = google_service_account.builder.email }
output "deployer_service_account" { value = google_service_account.deployer.email }
output "triggers" { value = local.github ? { pull_request = google_cloudbuild_trigger.pull_request[0].name, main = google_cloudbuild_trigger.main[0].name } : {} }
