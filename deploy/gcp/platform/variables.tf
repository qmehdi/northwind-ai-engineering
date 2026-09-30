# The one environment of the Google Cloud track (ADR 0008, 0009). Every name starts with
# `<environment>-<tenant>-`; `mode = "solo"` collapses the tenant list to the single tenant
# `solo`. Service account ids are capped at 30 characters by IAM, so they use the short form
# `nw-<tenant>-<kind>` while everything else carries the full prefix.

variable "project" {
  type        = string
  description = "Project id that holds the whole platform"
}

variable "project_number" {
  type        = string
  description = "Project number (gcloud projects describe <id> --format='value(projectNumber)'); names the Google service agents and the embedding publisher model without a data source, so `terraform plan` needs no credentials"
  validation {
    condition     = can(regex("^[0-9]{6,20}$", var.project_number))
    error_message = "project_number is the numeric project number."
  }
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "environment" {
  type        = string
  default     = "northwind"
  description = "The environment name that starts every resource name; a second deploy with another value is a second environment in the same project"
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,11}$", var.environment))
    error_message = "environment is 3 to 12 lowercase letters or digits, starting with a letter, so <environment>-<tenant>-<kind> fits every Google name limit."
  }
}

variable "mode" {
  type        = string
  default     = "cohort"
  description = "cohort: one tenant per learner from `tenants`; solo: the single tenant `solo`"
  validation {
    condition     = contains(["cohort", "solo"], var.mode)
    error_message = "mode is cohort or solo."
  }
}

variable "tenants" {
  type        = list(string)
  default     = []
  description = "Learner handles in cohort mode (2 to 16 lowercase letters or digits, starting with a letter; `live` is reserved for the promoted target)"
  validation {
    condition     = alltrue([for t in var.tenants : can(regex("^[a-z][a-z0-9]{1,15}$", t)) && !contains(["live", "platform", "gateway", "baselines", "agents", "monitoring", "clouddeploy", "audit"], t)])
    error_message = "each tenant is 2 to 16 lowercase letters or digits, starts with a letter, and is none of the reserved names live, platform, gateway, baselines, agents, monitoring, clouddeploy, audit."
  }
  validation {
    condition     = length(distinct(var.tenants)) == length(var.tenants)
    error_message = "tenants must be unique."
  }
}

variable "tenant_members" {
  type        = map(string)
  default     = {}
  description = "Tenant to Google account map (alice = \"alice@example.com\"): the account may impersonate its tenant identity service account nw-<tenant>-user, which is how a learner works in cohort mode"
}

variable "approvers" {
  type        = list(string)
  default     = []
  description = "Members (user:alice@example.com, group:...) who may impersonate <environment>-approvers, the only identity that writes the live runtime's approvals/ folder (claim markers, approval records, the escalation queue). The project owner can impersonate it without this list"
  validation {
    condition     = alltrue([for m in var.approvers : can(regex("^(user|group|serviceAccount):", m))])
    error_message = "each approver is a member string: user:<email>, group:<email> or serviceAccount:<email>."
  }
}

variable "organization_id" {
  type        = string
  default     = ""
  description = "Numeric organisation id; when set, the IAM deny policy on the platform secrets and the custom constraint on custom job machine types are created (both need organisation-level roles: roles/iam.denyAdmin and roles/orgpolicy.policyAdmin). Empty: the allow policies and the quota caps are the controls"
}

variable "secrets_generation" {
  type        = number
  default     = 1
  description = "Bump to rotate every generated secret on the next apply (the values are write-only and never in state); the gateway salt key never rotates"
}

variable "data_access_logs" {
  type        = bool
  default     = true
  description = "Data Access audit logs for Secret Manager and the Agent Platform (read and write), routed with Admin Activity into a 400 day log bucket"
}

variable "compute_quota_caps" {
  type        = bool
  default     = true
  description = "Quota overrides on the Agent Platform in the region: zero GPUs for training and serving, capped custom job and serving CPUs"
}

variable "training_cpu_quota" {
  type        = number
  default     = 0
  description = "Custom job CPUs in the region (every pipeline step is one, e2-standard-4 by default); 0 means 8 per tenant with a floor of 16"
}

variable "serving_cpu_quota" {
  type        = number
  default     = 16
  description = "Endpoint serving CPUs in the region: two live endpoints on n1-standard-2 with a canary and a stable version each need 8"
}

variable "allowed_machine_types" {
  type        = list(string)
  default     = ["e2-standard-2", "e2-standard-4", "e2-standard-8", "n1-standard-2", "n1-standard-4"]
  description = "Machine types custom jobs may use when the organisation constraint is on (organization_id)"
}

variable "rag_unprovision_on_destroy" {
  type        = bool
  default     = true
  description = "On destroy, set the project's RagManagedDb tier to Unprovisioned (ends its hourly charge and deletes every RagManagedDb corpus in the project); turn off when another environment in the project still uses RAG Engine"
}

variable "image_tag" {
  type        = string
  description = "Tag of the course images in Artifact Registry: the git SHA scripts/images_gcp.sh pushed (a dirty tree adds a hash of the diff). Tags are immutable and `latest` is never pushed"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{3,63}$", var.image_tag)) && var.image_tag != "latest"
    error_message = "image_tag is a git SHA tag such as abc1234 or abc1234-dirty-1a2b3c4d, never latest."
  }
}

variable "billing_account" {
  type        = string
  description = "Billing account id for the budget, like 012345-6789AB-CDEF01"
}

variable "budget_usd" {
  type    = number
  default = 300
}

variable "tenant_budget_usd" {
  type        = number
  default     = 25
  description = "Model spend cap per tenant on the gateway, reset every 30 days"
}

variable "alert_email" {
  type    = string
  default = ""
}

variable "cmek_key" {
  type        = string
  default     = ""
  description = "Optional Cloud KMS key (projects/../cryptoKeys/..) for the buckets and the BigQuery dataset; empty keeps Google-managed encryption"
}

variable "dataplex_catalog" {
  type        = bool
  default     = false
  description = "Register the tickets table in a Dataplex Universal Catalog entry group (off: the course does not need it, on: what a data governance team expects)"
}

variable "embedding_model" {
  type        = string
  default     = "text-embedding-005"
  description = "Publisher embedding model RAG Engine embeds with; its dimension (768) sizes the optional Vector Search index"
}

variable "rag_backend" {
  type        = string
  default     = "managed"
  description = "managed: RagManagedDb, no fixed cost; vector_search: a Vertex AI Vector Search index and endpoint per tenant, billed per node hour (deploy/COSTS-platform.md compares the two)"
  validation {
    condition     = contains(["managed", "vector_search"], var.rag_backend)
    error_message = "rag_backend is managed or vector_search."
  }
}

variable "rag_managed_db_tier" {
  type        = string
  default     = ""
  description = "Project-wide RagManagedDb tier: basic, scaled or unprovisioned; empty leaves the project default untouched"
  validation {
    condition     = contains(["", "basic", "scaled", "unprovisioned"], var.rag_managed_db_tier)
    error_message = "rag_managed_db_tier is empty, basic, scaled or unprovisioned."
  }
}

variable "live_models" {
  type        = list(string)
  default     = ["triage", "semantic"]
  description = "Registered model names that get a live Vertex endpoint with a traffic split"
}

variable "endpoint_id_base" {
  type        = number
  default     = 100000
  description = "Vertex endpoint ids must be numeric; live endpoints take base, base+1, ... in live_models order. Change it for a second environment in the same project"
}

variable "model_monitoring" {
  type        = bool
  default     = true
  description = "Create a Model Monitoring job on every live endpoint (a script: the provider has no resource for Model Monitoring v2)"
}

variable "public_services" {
  type        = bool
  default     = true
  description = "Cloud Run services take unauthenticated calls guarded by x-api-key (the guide's curl); false keeps them private to the agent's service account"
}

variable "scheduler_enabled" {
  type        = bool
  default     = false
  description = "Run the weekly retraining pipeline; the job exists paused otherwise"
}

variable "scheduler_cron" {
  type    = string
  default = "0 6 * * 1"
}

variable "gateway_image" {
  type = string
  # v1.103.0, the release the Local track pins (docker-compose.yml); the digest is the multi-arch
  # index ghcr.io serves for that tag (checked 2026-09-30).
  default     = "berriai/litellm@sha256:bd089afdcd35b894b14a93f9743cdc8b591f82da1a38dd43a010a7b0c9de5fd7"
  description = "LiteLLM image under ghcr.io, pulled through the remote repository (Cloud Run cannot pull ghcr.io directly), pinned by digest: the container holds the master key and the model credentials"
  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.gateway_image))
    error_message = "gateway_image is pinned by digest: <path>@sha256:<64 hex>."
  }
}

variable "gateway_database" {
  type        = bool
  default     = true
  description = "Cloud SQL Postgres (db-f1-micro) behind the gateway so per-tenant virtual keys and budgets work; false serves the master key only (solo mode can live with that)"
}

variable "gateway_models" {
  type = map(object({
    model    = string
    location = string
  }))
  default = {
    workhorse = { model = "vertex_ai/openai/gpt-oss-120b-maas", location = "us-central1" }
    # gpt-oss-20b-maas retires on Google on 2026-10-21 (nw/config.py), so Economy is the 120b
    # route with the Economy token caps until a cheaper open model is generally available.
    economy = { model = "vertex_ai/openai/gpt-oss-120b-maas", location = "us-central1" }
    judge   = { model = "vertex_ai/claude-opus-5", location = "us-east5" }
    # Residency: EU accounts route to `eu/<role>` (nw/config.py). Claude is served in the `eu`
    # multi-region; gpt-oss has no EU endpoint on Google, so eu/workhorse and eu/economy exist
    # only once an operator deploys gpt-oss to an EU endpoint, adds the entries here and sets
    # NW_MODEL_EU_WORKHORSE and NW_MODEL_EU_ECONOMY. Until then the client refuses those routes.
    "eu/judge" = { model = "vertex_ai/claude-opus-5", location = "eu" }
  }
  description = "Model route name to LiteLLM model and location (ADR 0010); the location is where the publisher model is served, `eu/<role>` routes serve EU accounts"
}

variable "github_owner" {
  type        = string
  default     = ""
  description = "GitHub owner of the course repository; empty skips Cloud Build triggers (the Cloud Deploy pipeline is created regardless)"
}

variable "github_repo" {
  type    = string
  default = "northwind-ai-engineering"
}

variable "github_branch" {
  type    = string
  default = "main"
}

variable "github_app_installation_id" {
  type        = number
  default     = 0
  description = "Installation id of the Cloud Build GitHub app on the owner (from the connection's action_uri on first apply)"
}

variable "github_oauth_token_secret_version" {
  type        = string
  default     = ""
  description = "projects/<p>/secrets/<s>/versions/<v> holding the GitHub OAuth token for the Cloud Build connection"
}

variable "canary_percent" {
  type        = number
  default     = 10
  description = "First canary phase of the Cloud Deploy pipeline; the remaining traffic moves after the manual approval"
  validation {
    condition     = var.canary_percent > 0 && var.canary_percent < 100
    error_message = "canary_percent is between 1 and 99."
  }
}
