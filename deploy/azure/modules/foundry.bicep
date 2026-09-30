// Microsoft Foundry: the Foundry resource (an AIServices account with project management on),
// one project, the three model deployments of the course roles, the content filter policy with
// Prompt Shields that every deployment carries, and the project's connections to Application
// Insights (tracing and Foundry evaluations) and to Azure AI Search (the agents' search tool).
// The account is the Azure Verified Module; its children are native because the module's
// project and deployment children are only "Proposed" in the AVM index (2026-09-29) and the
// deployments must reference a content filter policy created after the account.
//
// Foundry evaluations and the Agent Service's basic setup need no further resource: evaluation
// runs and agent versions are created in the project by nw/platform/azure.py through the SDK.
metadata owner = 'northwind'

param environment string
param location string
param tags object
param suffix string
param models array
param claudeOrganizationName string
param claudeCountryCode string
param claudeIndustry string
param localAuth bool
param appInsightsId string
@secure()
param appInsightsConnectionString string
param searchEndpoint string
param searchPrincipalId string
param searchName string
param acrName string
param logsWorkspaceId string
param enableTelemetry bool

var accountName = '${environment}-foundry-${suffix}'

module account 'br/public:avm/res/cognitive-services/account:0.19.1' = {
  name: '${environment}-foundry'
  params: {
    name: accountName
    kind: 'AIServices'
    sku: 'S0'
    location: location
    tags: tags
    // The subdomain is the endpoint host: https://<name>.services.ai.azure.com, globally unique.
    customSubDomainName: accountName
    allowProjectManagement: true
    // Entra ID only unless localAuth: every caller is a managed identity or a signed-in user;
    // API Management reaches the models with its own identity.
    disableLocalAuth: !localAuth
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

// Content filters with Prompt Shields: user prompt attacks (`Jailbreak`) and document attacks
// (`Indirect Attack`) are blocked on the prompt side, the four harm categories at Medium on both
// sides, protected material text on the completion side. The guide's red-team step reads this
// policy; `nw` reads the Prompt Shields verdict from the response's content filter results.
var harmFilters = [
  for f in [
    { name: 'Hate', source: 'Prompt' }
    { name: 'Hate', source: 'Completion' }
    { name: 'Sexual', source: 'Prompt' }
    { name: 'Sexual', source: 'Completion' }
    { name: 'Violence', source: 'Prompt' }
    { name: 'Violence', source: 'Completion' }
    { name: 'Selfharm', source: 'Prompt' }
    { name: 'Selfharm', source: 'Completion' }
  ]: {
    name: f.name
    source: f.source
    enabled: true
    blocking: true
    severityThreshold: 'Medium'
  }
]

// Prompt Shields and protected material take no severity.
var shieldFilters = [
  { name: 'Jailbreak', source: 'Prompt', enabled: true, blocking: true }
  { name: 'Indirect Attack', source: 'Prompt', enabled: true, blocking: true }
  { name: 'Protected Material Text', source: 'Completion', enabled: true, blocking: true }
]

resource shields 'Microsoft.CognitiveServices/accounts/raiPolicies@2026-07-01' = {
  parent: foundry
  name: '${environment}-shields'
  properties: {
    basePolicyName: 'Microsoft.DefaultV2'
    mode: 'Default'
    contentFilters: concat(harmFilters, shieldFilters)
  }
}

// The three roles of ADR 0010 and the embedding model of the search indexes. The deployment name is the model id, which is what
// nw/config.py sends as `model`. One at a time: Foundry serialises deployments on an account.
@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = [
  for m in models: {
    parent: foundry
    name: m.name
    sku: {
      name: m.sku
      capacity: m.capacity
    }
    properties: union(
      {
        model: {
          format: m.format
          name: m.name
          version: m.version
        }
        raiPolicyName: shields.name
        versionUpgradeOption: 'NoAutoUpgrade'
      },
      // Claude on Foundry is sold through Azure Marketplace and needs the buyer's details.
      m.format == 'Anthropic'
        ? {
            modelProviderData: {
              organizationName: claudeOrganizationName
              countryCode: claudeCountryCode
              industry: claudeIndustry
            }
          }
        : {}
    )
  }
]

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-07-01' = {
  parent: foundry
  name: '${environment}-project'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: '${environment} course project'
    description: 'Prompts, agents, evaluations and traces for every tenant; names carry the tenant prefix'
  }
  dependsOn: [deployments]
}

// Tracing and Foundry evaluations write to the platform's Application Insights.
resource tracing 'Microsoft.CognitiveServices/accounts/projects/connections@2026-07-01' = {
  parent: project
  name: '${environment}-appinsights'
  properties: {
    category: 'AppInsights'
    target: appInsightsId
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsightsConnectionString
    }
    metadata: {
      ResourceId: appInsightsId
    }
  }
}

// The agents' Azure AI Search tool reaches the indexes with the project's identity. Known
// limit: the project is shared, and so is this connection and the identity's reader role on
// the whole service, so any agent in the project (any tenant's) can read every tenant's
// index. Tenants' own identities read only their own index (deploy_azure.sh indexes); a
// project per tenant is the organisation's answer.
resource search 'Microsoft.CognitiveServices/accounts/projects/connections@2026-07-01' = {
  parent: project
  name: '${environment}-search'
  properties: {
    category: 'CognitiveSearch'
    target: searchEndpoint
    authType: 'AAD'
    isSharedToAll: true
    metadata: {
      ResourceId: searchService.id
      ApiType: 'Azure'
    }
  }
}

resource searchService 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchName
}

// Search Index Data Reader for the project, so an agent's search tool reads the indexes.
resource projectReadsSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(searchService.id, project.id, '1407120a-92aa-4202-b7e9-c0e197c71c8f')
  scope: searchService
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '1407120a-92aa-4202-b7e9-c0e197c71c8f'
    )
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Integrated vectorization: the index's vectorizer calls the embedding deployment as the search
// service's identity (Cognitive Services OpenAI User).
resource searchEmbeds 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, searchName, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
    )
    principalId: searchPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: acrName
}

// A tenant's rights in the shared project: read it, build agents and run evaluations (the
// `AIServices/agents` and `AIServices/evaluations` data actions, named on the Foundry pages
// "Role-based access control for Microsoft Foundry" and "Disable preview features",
// 2026-09-30). Foundry User would add every data action of the account, model inference
// included, which would let a tenant call the deployments past the gateway's key, token limit
// and quota; this role leaves inference out. The remaining path, a known limit: an agent
// built in the project runs on the account's deployments, so its tokens are billed to the
// project, not to the tenant's gateway quota (deploy/azure/README.md, "Identity and security
// notes").
resource tenantProjectRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, environment, 'tenant-project')
  properties: {
    roleName: '${environment} tenant on the Foundry project (${resourceGroup().name})'
    description: 'Northwind course: agents and evaluations in the shared Foundry project, no direct model inference'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [
      {
        actions: [
          'Microsoft.CognitiveServices/*/read'
          'Microsoft.Authorization/*/read'
        ]
        notActions: []
        dataActions: [
          'Microsoft.CognitiveServices/accounts/AIServices/agents/*'
          'Microsoft.CognitiveServices/accounts/AIServices/evaluations/*'
        ]
        notDataActions: []
      }
    ]
  }
}

// If nw/platform/azure.py deploys a Foundry hosted agent, the project pulls the agent image from
// the platform registry (hosted agents page: Container Registry Repository Reader; AcrPull for a
// registry in the default permissions mode).
resource projectPullsImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for role in ['b93aa761-3e63-49ed-ac28-beffa264f7ac', '7f951dda-4ed3-4680-a7ca-43fe172d538d']: {
    name: guid(registry.id, project.id, role)
    scope: registry
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
      principalId: project.identity.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

output accountName string = foundry.name
output accountId string = foundry.id
output endpoint string = 'https://${accountName}.services.ai.azure.com'
// Content Safety (Prompt Shields) is part of the AIServices account, on the custom subdomain's
// cognitiveservices host; nw.agent.screen.AzurePromptShields calls text:shieldPrompt there.
output contentSafetyEndpoint string = 'https://${accountName}.cognitiveservices.azure.com'
output projectName string = project.name
output projectId string = project.id
output projectEndpoint string = 'https://${accountName}.services.ai.azure.com/api/projects/${project.name}'
output projectPrincipalId string = project.identity.principalId
output accountPrincipalId string = account.outputs.systemAssignedMIPrincipalId!
output raiPolicyName string = shields.name
output contentFilters array = concat(harmFilters, shieldFilters)
output tenantProjectRoleId string = tenantProjectRole.id
output deploymentNames array = [for (m, i) in models: deployments[i].name]
