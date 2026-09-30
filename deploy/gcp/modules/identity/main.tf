# Identity: who a learner is on the platform and what each workload may do (ADR 0009).
#
# - The tenant identity `nw-<tenant>-user`: what a learner acts as (a learner in `tenant_members`
#   impersonates it; solo mode is the project owner and uses it for rehearsal). It carries the
#   custom role `<environment>_tenant`, which is `roles/aiplatform.user` without the dangerous
#   parts: no `endpoints.predict` (every model call goes through the gateway with the tenant's
#   key and budget), no `reasoningEngines.create|update|delete` (engines are created by
#   Terraform; a tenant updates only its own engine, granted on that engine in modules/agents),
#   no custom, tuning, batch, notebook, index or endpoint creation (compute a tenant could size
#   freely). Storage, Cloud Run and service account grants sit next to the resources they are on.
# - Custom roles for the workloads, all curated from the permission list of
#   `roles/aiplatform.user` (IAM roles reference, fetched 2026-09-30): pipelines, the agent
#   runtime, RAG retrieval, the live drill.
# - The per-tenant API key of the tenant's services (`<environment>-<tenant>-api-key`), so a
#   learner's key opens that learner's services only.
# - The tag `<project>/<environment>-secret-class` on every secret: `platform` (the gateway's
#   master key, salt, config and database URL, read by the gateway only) or `service` (API and
#   gateway keys a workload or tenant reads when granted). The IAM deny policy in
#   modules/guardrails keys on it.
variable "project" { type = string }
variable "environment" { type = string }
variable "labels" { type = map(string) }
variable "tenants" { type = list(string) }
variable "tenant_members" { type = map(string) }
variable "secrets_generation" { type = number }

locals {
  metadata = [
    "aiplatform.artifacts.create", "aiplatform.artifacts.get", "aiplatform.artifacts.list", "aiplatform.artifacts.update",
    "aiplatform.contexts.addContextArtifactsAndExecutions", "aiplatform.contexts.addContextChildren",
    "aiplatform.contexts.create", "aiplatform.contexts.get", "aiplatform.contexts.list",
    "aiplatform.contexts.queryContextLineageSubgraph", "aiplatform.contexts.update",
    "aiplatform.executions.addExecutionEvents", "aiplatform.executions.create", "aiplatform.executions.get",
    "aiplatform.executions.list", "aiplatform.executions.queryExecutionInputsAndOutputs", "aiplatform.executions.update",
    "aiplatform.metadataSchemas.create", "aiplatform.metadataSchemas.get", "aiplatform.metadataSchemas.list",
    "aiplatform.metadataStores.create", "aiplatform.metadataStores.get", "aiplatform.metadataStores.list",
    "aiplatform.tensorboards.create", "aiplatform.tensorboards.get", "aiplatform.tensorboards.list",
    "aiplatform.tensorboardExperiments.create", "aiplatform.tensorboardExperiments.get",
    "aiplatform.tensorboardExperiments.list", "aiplatform.tensorboardExperiments.update", "aiplatform.tensorboardExperiments.write",
    "aiplatform.tensorboardRuns.batchCreate", "aiplatform.tensorboardRuns.create", "aiplatform.tensorboardRuns.get",
    "aiplatform.tensorboardRuns.list", "aiplatform.tensorboardRuns.update", "aiplatform.tensorboardRuns.write",
    "aiplatform.tensorboardTimeSeries.batchCreate", "aiplatform.tensorboardTimeSeries.batchRead",
    "aiplatform.tensorboardTimeSeries.create", "aiplatform.tensorboardTimeSeries.get", "aiplatform.tensorboardTimeSeries.list",
    "aiplatform.tensorboardTimeSeries.read", "aiplatform.tensorboardTimeSeries.update",
  ]
  base     = ["resourcemanager.projects.get", "aiplatform.locations.get", "aiplatform.locations.list", "aiplatform.operations.list"]
  registry = ["aiplatform.models.get", "aiplatform.models.list", "aiplatform.models.upload", "aiplatform.models.update", "aiplatform.modelEvaluations.get", "aiplatform.modelEvaluations.list", "aiplatform.modelEvaluations.import"]
  runs     = ["aiplatform.pipelineJobs.cancel", "aiplatform.pipelineJobs.create", "aiplatform.pipelineJobs.get", "aiplatform.pipelineJobs.list"]
  rag_read = ["aiplatform.ragCorpora.get", "aiplatform.ragCorpora.list", "aiplatform.ragCorpora.query", "aiplatform.ragFiles.get", "aiplatform.ragFiles.list", "aiplatform.ragEngineConfigs.get"]

  roles = {
    # What a learner does from a laptop or notebook (the tenant workflow in the README).
    tenant = {
      title = "Northwind tenant"
      permissions = concat(local.base, local.metadata, local.registry, local.runs, local.rag_read, [
        "aiplatform.ragFiles.import", "aiplatform.ragFiles.upload", "aiplatform.ragFiles.delete",
        "aiplatform.endpoints.get", "aiplatform.endpoints.list",
        "aiplatform.reasoningEngines.get", "aiplatform.reasoningEngines.list",
        # prompt management keeps prompt versions as datasets
        "aiplatform.datasets.create", "aiplatform.datasets.get", "aiplatform.datasets.list", "aiplatform.datasets.update",
        "aiplatform.datasetVersions.create", "aiplatform.datasetVersions.get", "aiplatform.datasetVersions.list",
        "aiplatform.datasetVersions.restore",
      ])
    }
    # Granted on one reasoning engine only (modules/agents): deploy a new image and query it.
    tenant_engine = {
      title       = "Northwind tenant engine"
      permissions = ["aiplatform.reasoningEngines.get", "aiplatform.reasoningEngines.query", "aiplatform.reasoningEngines.update"]
    }
    # Granted on the live endpoints only (modules/live): the promotion drill's canary.
    live_deployer = {
      title       = "Northwind live deployer"
      permissions = ["aiplatform.endpoints.get", "aiplatform.endpoints.deploy", "aiplatform.endpoints.undeploy", "aiplatform.endpoints.update"]
    }
    # Pipeline steps: run as custom jobs, log lineage, register the candidate.
    pipelines = {
      title = "Northwind pipelines"
      permissions = concat(local.base, local.metadata, local.registry, local.runs, [
        "aiplatform.customJobs.cancel", "aiplatform.customJobs.create", "aiplatform.customJobs.get", "aiplatform.customJobs.list",
      ])
    }
    # The resolver on Agent Engine: sessions and Memory Bank, no model access (the gateway is).
    agent_runtime = {
      title = "Northwind agent runtime"
      permissions = concat(local.base, [
        "aiplatform.reasoningEngines.get",
        "aiplatform.sessions.create", "aiplatform.sessions.delete", "aiplatform.sessions.get", "aiplatform.sessions.list",
        "aiplatform.sessions.update", "aiplatform.sessionEvents.append", "aiplatform.sessionEvents.list",
        "aiplatform.memories.create", "aiplatform.memories.delete", "aiplatform.memories.generate", "aiplatform.memories.get",
        "aiplatform.memories.list", "aiplatform.memories.retrieve", "aiplatform.memories.update",
      ])
    }
    # The policy services: retrieval from a RAG Engine corpus (RAG Engine embeds with its own
    # service agent, so no predict permission is needed here).
    rag_reader = {
      title       = "Northwind RAG reader"
      permissions = concat(local.base, local.rag_read)
    }
  }
}

resource "google_project_iam_custom_role" "roles" {
  for_each    = local.roles
  project     = var.project
  role_id     = "${var.environment}_${each.key}"
  title       = "${each.value.title} (${var.environment})"
  description = "Curated from roles/aiplatform.user; no direct model predict, no reasoning engine creation"
  permissions = sort(distinct(each.value.permissions))
}

# ----- the tenant identity ----------------------------------------------------------------------

resource "google_service_account" "user" {
  for_each     = toset(var.tenants)
  project      = var.project
  account_id   = "nw-${each.key}-user"
  display_name = "${var.environment}-${each.key} tenant identity"
}

locals {
  # Read-only views a learner needs for `make runs-gcp`, the dashboard and the tickets query.
  # The platform logs carry no ticket text (docs/SECURITY.md), so a shared log view is safe.
  tenant_project_roles = ["roles/logging.viewer", "roles/monitoring.viewer", "roles/bigquery.jobUser", "roles/serviceusage.serviceUsageConsumer"]
  tenant_role_pairs = {
    for pair in setproduct(var.tenants, local.tenant_project_roles) : "${pair[0]}:${pair[1]}" => { tenant = pair[0], role = pair[1] }
  }
}

resource "google_project_iam_member" "tenant" {
  for_each = toset(var.tenants)
  project  = var.project
  role     = google_project_iam_custom_role.roles["tenant"].id
  member   = "serviceAccount:${google_service_account.user[each.key].email}"
}

resource "google_project_iam_member" "tenant_views" {
  for_each = local.tenant_role_pairs
  project  = var.project
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.user[each.value.tenant].email}"
}

resource "google_service_account_iam_member" "impersonate" {
  for_each           = { for t, member in var.tenant_members : t => member if contains(var.tenants, t) }
  service_account_id = google_service_account.user[each.key].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "user:${each.value}"
}

# ----- secret classes for the deny policy -----------------------------------------------------------

resource "google_tags_tag_key" "secret_class" {
  parent      = "projects/${var.project}"
  short_name  = "${var.environment}-secret-class"
  description = "Who may read a secret of ${var.environment}: platform (the gateway only) or service (a workload or tenant, when granted)"
}

resource "google_tags_tag_value" "secret_class" {
  for_each    = toset(["platform", "service"])
  parent      = google_tags_tag_key.secret_class.id
  short_name  = each.key
  description = "Secrets of class ${each.key}"
}

# ----- the per-tenant service API key ---------------------------------------------------------------
# Write-only: the value never reaches the state. Rotate with secrets_generation (or
# scripts/rotate_key.sh, which adds a version out of band).

ephemeral "random_password" "api_key" {
  for_each = toset(var.tenants)
  length   = 40
  special  = false
}

resource "google_secret_manager_secret" "api_key" {
  for_each  = toset(var.tenants)
  project   = var.project
  secret_id = "${var.environment}-${each.key}-api-key"
  labels    = merge(var.labels, { area = "identity", tenant = each.key })
  tags      = { (google_tags_tag_key.secret_class.id) = google_tags_tag_value.secret_class["service"].id }
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "api_key" {
  for_each               = toset(var.tenants)
  secret                 = google_secret_manager_secret.api_key[each.key].id
  secret_data_wo         = ephemeral.random_password.api_key[each.key].result
  secret_data_wo_version = tostring(var.secrets_generation)
}

resource "google_secret_manager_secret_iam_member" "api_key" {
  for_each  = toset(var.tenants)
  project   = var.project
  secret_id = google_secret_manager_secret.api_key[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.user[each.key].email}"
}

output "roles" { value = { for k, r in google_project_iam_custom_role.roles : k => r.id } }
output "user_accounts" { value = { for k, sa in google_service_account.user : k => sa.email } }
output "tenant_members" { value = { for t, m in var.tenant_members : t => m if contains(var.tenants, t) } }
output "api_key_secrets" { value = { for k, s in google_secret_manager_secret.api_key : k => s.secret_id } }
output "secret_class_key" { value = google_tags_tag_key.secret_class.id }
output "secret_class_key_namespaced" { value = google_tags_tag_key.secret_class.namespaced_name }
output "secret_class_values" { value = { for k, v in google_tags_tag_value.secret_class : k => v.id } }
