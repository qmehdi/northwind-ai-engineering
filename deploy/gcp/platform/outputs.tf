output "environment" { value = var.environment }
output "mode" { value = var.mode }
output "tenants" {
  description = "Everything a tenant needs to know, by handle"
  value = {
    for t in local.tenants : t => {
      prefix                 = "${var.environment}-${t}"
      services               = { for k, v in module.serving.urls : k => v if startswith(k, "${t}-") }
      agent_engine           = module.agents.engines[t]
      agent_service_account  = module.agents.service_accounts[t]
      pipelines_sa           = module.tracking.service_accounts[t]
      identity_sa            = module.gateway.identity_accounts[t]
      gateway_key_secret     = module.gateway.tenant_key_secrets[t]
      rag_corpus             = module.prompts.corpora[t]
      retrain_scheduler_job  = module.tracking.scheduler_jobs[t]
      pipeline_root          = "gs://${module.data.pipelines_bucket}/${var.environment}-${t}"
      model_artifacts_prefix = "gs://${module.data.artifacts_bucket}/${var.environment}-${t}"
    }
  }
}
output "gateway_url" { value = module.gateway.url }
output "gateway_master_key_secret" { value = module.gateway.master_key_secret }
output "buckets" {
  value = {
    data      = module.data.data_bucket
    artifacts = module.data.artifacts_bucket
    pipelines = module.data.pipelines_bucket
  }
}
output "bigquery_tickets" { value = module.data.tickets_table }
output "live_endpoints" { value = module.live.endpoint_ids }
output "live_service_account" { value = module.live.service_account }
output "model_armor_template" { value = module.agents.model_armor_template }
output "agent_registry" { value = module.agents.registry_uri }
output "delivery_pipeline" { value = module.delivery.pipeline }
output "delivery_target" { value = module.delivery.target }
output "deployer_service_account" { value = module.delivery.deployer_service_account }
output "image_registry" { value = local.registry }
output "dashboard" { value = module.observability.dashboard }
output "organization_policies" {
  description = "Constraints an organisation would set at the folder; listed here, not applied (the course has no organisation)"
  value       = module.observability.organization_policies
}
output "api_key" {
  value     = random_password.api_key.result
  sensitive = true
}
