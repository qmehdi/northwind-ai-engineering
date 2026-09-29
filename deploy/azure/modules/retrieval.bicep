// Retrieval: one Azure AI Search service for the platform. Indexes are data plane (there is no
// ARM resource for an index), so scripts/deploy_azure.sh creates `northwind-<tenant>-policies`
// per tenant and for `live` from deploy/azure/search/policies-index.json and assigns Search
// Index Data Contributor on that index alone to the tenant's identity. Basic holds 15 indexes
// and Standard S1 holds 50 (search-limits-quotas-capacity, 2026-09-25), so main.bicep picks S1
// when the cohort has more than 14 tenants. Keys are off: every caller uses Entra ID.
metadata owner = 'northwind'

param environment string
param location string
param tags object
param suffix string
param sku string
param logsWorkspaceId string
param enableTelemetry bool

module search 'br/public:avm/res/search/search-service:0.13.0' = {
  name: '${environment}-search'
  params: {
    // Search names are 2 to 60 lowercase letters, digits or dashes and form a global host name.
    name: '${environment}-search-${suffix}'
    location: location
    tags: tags
    sku: sku
    replicaCount: 1
    partitionCount: 1
    disableLocalAuth: true
    semanticSearch: 'free'
    publicNetworkAccess: 'Enabled'
    managedIdentities: {
      systemAssigned: true
    }
    diagnosticSettings: [
      {
        name: 'to-logs'
        workspaceResourceId: logsWorkspaceId
      }
    ]
    enableTelemetry: enableTelemetry
  }
}

output name string = search.outputs.name
output id string = search.outputs.resourceId
output endpoint string = search.outputs.endpoint
output principalId string = search.outputs.systemAssignedMIPrincipalId!
