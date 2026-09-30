// The EU Foundry resource: where calls for EU accounts go (nw/config.py `EU_MODELS`, the
// residency rule). Global Standard deployments may process anywhere, and neither gpt-oss-120b,
// mistral-small-2503 nor Claude has an EU Data Zone deployment on Foundry, so the EU side runs
// Mistral-Large-3 on `DataZoneStandard` (EU data zone) for the Workhorse and Economy roles.
// There is no EU Judge on this track: nw refuses an EU call for the Judge by design.
//
// The course client calls this resource directly with Entra ID (NW_AZURE_FOUNDRY_EU_ENDPOINT,
// nw/llm/providers), not through API Management, so every owner's identity (and learner) holds
// Cognitive Services User here, on this resource only. The known limit: EU calls are bounded by
// the deployment's tokens-per-minute capacity, not by the gateway's per-tenant quota; an
// organisation fronts it with an EU API Management instance.
metadata owner = 'northwind'

param environment string
param location string
param tags object
param suffix string
param models array
param contentFilters array
param principals array
param logsWorkspaceId string
param enableTelemetry bool

var accountName = '${environment}-foundry-eu-${suffix}'
var cognitiveServicesUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'

module account 'br/public:avm/res/cognitive-services/account:0.19.1' = {
  name: '${environment}-foundry-eu'
  params: {
    name: accountName
    kind: 'AIServices'
    sku: 'S0'
    location: location
    tags: tags
    customSubDomainName: accountName
    disableLocalAuth: true
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

resource foundry 'Microsoft.CognitiveServices/accounts@2026-07-01' existing = {
  name: accountName
  dependsOn: [account]
}

resource shields 'Microsoft.CognitiveServices/accounts/raiPolicies@2026-07-01' = {
  parent: foundry
  name: '${environment}-shields'
  properties: {
    basePolicyName: 'Microsoft.DefaultV2'
    mode: 'Default'
    contentFilters: contentFilters
  }
}

@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = [
  for m in models: {
    parent: foundry
    name: m.name
    sku: {
      name: m.sku
      capacity: m.capacity
    }
    properties: {
      model: {
        format: m.format
        name: m.name
        version: m.version
      }
      raiPolicyName: shields.name
      versionUpgradeOption: 'NoAutoUpgrade'
    }
  }
]

resource callsModels 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in principals: {
    name: guid(foundry.id, p.id, cognitiveServicesUser)
    scope: foundry
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUser)
      principalId: p.id
      principalType: p.type
    }
  }
]

output endpoint string = 'https://${accountName}.services.ai.azure.com'
output accountName string = accountName
