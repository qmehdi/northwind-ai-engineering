# Guardrails that sit above the allow policies: audit logging and retention, the IAM deny
# policy on the platform's secrets, and the compute limits for anything a tenant can start.
#
# - Data Access audit logs for Secret Manager and the Agent Platform (who read which secret, who
#   called which model or engine), routed with the Admin Activity logs to the log bucket
#   `<environment>-audit`, kept 400 days (the retention table in docs/governance).
# - IAM deny (needs an organisation: creating a deny policy takes roles/iam.denyAdmin on the
#   organisation, so it is created only when organization_id is set; the allow side already
#   grants nobody but the gateway these secrets). Two rules: the Reasoning Engine service agent
#   reads no secret at all (engines carry no secret_env, modules/agents), and no tenant or
#   workload identity reads a secret tagged `<project>/<environment>-secret-class=platform`
#   (the gateway master key, salt key, config and database URL).
# - Compute limits. Quota overrides on the Agent Platform: no GPUs for training or serving, and
#   capped CPUs for custom jobs (every pipeline step is one) and for endpoint serving. They cap
#   the project, which is the one thing a project without an organisation can enforce. With an
#   organisation, a custom constraint also denies custom jobs on any machine type outside
#   `allowed_machine_types` or with accelerators, enforced on this project.
variable "project" { type = string }
variable "project_number" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "organization_id" { type = string }
variable "data_access_logs" { type = bool }
variable "compute_quota_caps" { type = bool }
variable "training_cpu_quota" { type = number }
variable "serving_cpu_quota" { type = number }
variable "allowed_machine_types" { type = list(string) }
variable "secret_class_key_namespaced" { type = string }
variable "denied_service_accounts" {
  type        = list(string)
  description = "Every tenant and workload service account email that must never read a platform secret"
}
variable "denied_users" {
  type        = list(string)
  description = "Learner Google accounts (tenant_members)"
}

locals {
  org                    = var.organization_id != ""
  reasoning_engine_agent = "service-${var.project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  gpus                   = ["nvidia_t4", "nvidia_l4", "nvidia_p4", "nvidia_p100", "nvidia_v100", "nvidia_a100", "nvidia_a100_80gb", "nvidia_h100"]
  quotas = merge(
    { training_cpus = { metric = "custom_model_training_cpus", value = var.training_cpu_quota } },
    { serving_cpus = { metric = "custom_model_serving_cpus", value = var.serving_cpu_quota } },
    { for g in local.gpus : "training_${g}" => { metric = "custom_model_training_${g}_gpus", value = 0 } },
    { for g in local.gpus : "serving_${g}" => { metric = "custom_model_serving_${g}_gpus", value = 0 } },
  )
}

# ----- audit logs ------------------------------------------------------------------------------------

resource "google_project_iam_audit_config" "data_access" {
  for_each = var.data_access_logs ? toset(["secretmanager.googleapis.com", "aiplatform.googleapis.com"]) : toset([])
  project  = var.project
  service  = each.value
  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

resource "google_logging_project_bucket_config" "audit" {
  project        = var.project
  location       = "global"
  bucket_id      = "${var.environment}-audit"
  retention_days = 400
  description    = "Audit logs of ${var.environment} (Admin Activity and Data Access), kept 400 days"
}

resource "google_logging_project_sink" "audit" {
  project     = var.project
  name        = "${var.environment}-audit"
  destination = "logging.googleapis.com/${google_logging_project_bucket_config.audit.id}"
  filter      = "logName:\"cloudaudit.googleapis.com\""
  description = "Every audit log entry of the project into the 400 day bucket"
}

# ----- IAM deny on the platform secrets (organisation only) -----------------------------------------

resource "google_iam_deny_policy" "secrets" {
  count        = local.org ? 1 : 0
  parent       = urlencode("cloudresourcemanager.googleapis.com/projects/${var.project}")
  name         = "${var.environment}-secrets"
  display_name = "${var.environment}: platform secrets stay with the gateway"

  rules {
    description = "The Reasoning Engine service agent reads no secret: engines carry no secret_env"
    deny_rule {
      denied_principals  = ["principal://iam.googleapis.com/projects/-/serviceAccounts/${local.reasoning_engine_agent}"]
      denied_permissions = ["secretmanager.googleapis.com/versions.access"]
    }
  }

  rules {
    description = "No tenant or workload identity reads a platform secret (gateway master key, salt, database URL)"
    deny_rule {
      denied_principals = concat(
        [for e in var.denied_service_accounts : "principal://iam.googleapis.com/projects/-/serviceAccounts/${e}"],
        [for u in var.denied_users : "principal://goog/subject/${u}"],
      )
      denied_permissions = ["secretmanager.googleapis.com/versions.access", "secretmanager.googleapis.com/versions.get", "secretmanager.googleapis.com/secrets.setIamPolicy"]
      denial_condition {
        title      = "platform secrets"
        expression = "resource.matchTag('${var.secret_class_key_namespaced}', 'platform')"
      }
    }
  }
}

# ----- compute limits ---------------------------------------------------------------------------------

resource "google_service_usage_consumer_quota_override" "vertex" {
  provider       = google-beta
  for_each       = var.compute_quota_caps ? local.quotas : {}
  project        = var.project
  service        = "aiplatform.googleapis.com"
  metric         = urlencode("aiplatform.googleapis.com/${each.value.metric}")
  limit          = urlencode("/project/region")
  dimensions     = { region = var.region }
  override_value = tostring(each.value.value)
  force          = true
}

resource "google_org_policy_custom_constraint" "machine_types" {
  count          = local.org ? 1 : 0
  parent         = "organizations/${var.organization_id}"
  name           = "custom.${var.environment}JobMachineTypes"
  display_name   = "${var.environment}: custom jobs on the allowed machine types, no accelerators"
  description    = "Custom jobs (every pipeline step) run on ${join(", ", var.allowed_machine_types)} without accelerators."
  action_type    = "DENY"
  method_types   = ["CREATE"]
  resource_types = ["aiplatform.googleapis.com/CustomJob"]
  condition      = "resource.jobSpec.workerPoolSpecs.exists(spec, !(spec.machineSpec.machineType in ${jsonencode(var.allowed_machine_types)}) || spec.machineSpec.acceleratorCount > 0)"
}

resource "google_org_policy_policy" "machine_types" {
  count  = local.org ? 1 : 0
  name   = "projects/${var.project}/policies/${google_org_policy_custom_constraint.machine_types[0].name}"
  parent = "projects/${var.project}"
  spec {
    rules {
      enforce = "TRUE"
    }
  }
}

output "audit_bucket" { value = google_logging_project_bucket_config.audit.id }
output "deny_policy" { value = local.org ? google_iam_deny_policy.secrets[0].name : "not created: needs organization_id and roles/iam.denyAdmin on the organisation" }
output "quota_overrides" { value = { for k, q in google_service_usage_consumer_quota_override.vertex : k => q.override_value } }
