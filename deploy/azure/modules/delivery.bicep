// Delivery: two identities, so that building and releasing are different rights.
//
//   <environment>-builder   pushes images (AcrPush, and Reader on the registry for the login)
//                           and reads the deployments for outputs.json. It is the identity a
//                           push to `main` gets (GitHub `ref:refs/heads/main`, or the Azure
//                           Pipelines `builder` service connection): a branch push can build,
//                           never release.
//   <environment>-deployer  releases: a custom role assigned on the live apps only (not on
//                           any tenant's app, so no listSecrets there; the live apps' secrets
//                           are Key Vault references, so listSecrets returns their URLs, not
//                           values), the live identity to assign, the environment to join,
//                           the endpoint role on the live online endpoints, reading the
//                           workspace, and reading the alerts the canary gate checks. It
//                           trusts only the GitHub environments `<environment>-canary` and
//                           `<environment>-live` (restrict both to the `main` branch; required
//                           reviewers on `-live`), or the Azure Pipelines `deployer` connection.
//
// Signing: the builder signs every image it pushes with cosign and the Key Vault key
// `<environment>-image-signing` (`azurekms://`, scripts/images_azure.sh); the deployer reads the
// public key and `deploy_azure.sh release` verifies the digest before any revision is created,
// failing closed on a missing or bad signature. Only the builder can sign.
//
// The manual approval is an Azure Pipelines environment check (or a GitHub environment rule):
// those live in the DevOps project, not in ARM, and deploy/azure/README.md says where to click.
metadata owner = 'northwind'

param environment string
param location string
param tags object
param acrName string
param githubRepository string
param azureDevOpsIssuer string
param azureDevOpsSubject string
param azureDevOpsBuilderSubject string
param liveAppNames array
param liveIdentityName string
param appsEnvironmentName string
param workspaceName string
param liveEndpointNames array
param endpointRoleId string
param keyVaultName string
param enableTelemetry bool

var acrPush = '8311e382-0749-4cb8-b61a-304f252e45ec'
var reader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
var cryptoUser = '12338af0-0e69-4776-bea7-57ae8d297424' // Key Vault Crypto User: sign
var cryptoReader = 'e147488a-f6f5-4113-8e2d-b22465e65bf6' // Key Vault Crypto Service Encryption User: read the key
var github = 'https://token.actions.githubusercontent.com'
var exchange = ['api://AzureADTokenExchange']
var deployerCredentials = concat(
  empty(githubRepository)
    ? []
    : [
        {
          name: 'github-${environment}-canary'
          issuer: github
          subject: 'repo:${githubRepository}:environment:${environment}-canary'
          audiences: exchange
        }
        {
          name: 'github-${environment}-live'
          issuer: github
          subject: 'repo:${githubRepository}:environment:${environment}-live'
          audiences: exchange
        }
      ],
  empty(azureDevOpsIssuer) || empty(azureDevOpsSubject)
    ? []
    : [
        {
          name: 'azure-pipelines'
          issuer: azureDevOpsIssuer
          subject: azureDevOpsSubject
          audiences: exchange
        }
      ]
)
var builderCredentials = concat(
  empty(githubRepository)
    ? []
    : [
        {
          name: 'github-main'
          issuer: github
          subject: 'repo:${githubRepository}:ref:refs/heads/main'
          audiences: exchange
        }
      ],
  empty(azureDevOpsIssuer) || empty(azureDevOpsBuilderSubject)
    ? []
    : [
        {
          name: 'azure-pipelines-builder'
          issuer: azureDevOpsIssuer
          subject: azureDevOpsBuilderSubject
          audiences: exchange
        }
      ]
)

module deployer 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: '${environment}-deployer'
  params: {
    name: '${environment}-deployer'
    location: location
    tags: tags
    federatedIdentityCredentials: deployerCredentials
    enableTelemetry: enableTelemetry
  }
}

module builder 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: '${environment}-builder'
  params: {
    name: '${environment}-builder'
    location: location
    tags: tags
    federatedIdentityCredentials: builderCredentials
    enableTelemetry: enableTelemetry
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: acrName
}

resource liveApps 'Microsoft.App/containerApps@2026-01-01' existing = [
  for n in liveAppNames: {
    name: n
  }
]

resource liveIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: liveIdentityName
}

resource appsEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: appsEnvironmentName
}

resource ml 'Microsoft.MachineLearningServices/workspaces@2026-05-01' existing = {
  name: workspaceName
}

resource liveEndpoints 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints@2026-05-01' existing = [
  for n in liveEndpointNames: {
    parent: ml
    name: n
  }
]

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' existing = {
  name: keyVaultName
}

resource signingKey 'Microsoft.KeyVault/vaults/keys@2026-02-01' = {
  parent: vault
  name: '${environment}-image-signing'
  tags: tags
  properties: {
    kty: 'EC'
    curveName: 'P-256'
    keyOps: ['sign', 'verify']
  }
}

resource builderSigns 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(signingKey.id, '${environment}-builder', cryptoUser)
  scope: signingKey
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cryptoUser)
    principalId: builder.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

resource deployerVerifies 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(signingKey.id, '${environment}-deployer', cryptoReader)
  scope: signingKey
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cryptoReader)
    principalId: deployer.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// ----- the builder ----------------------------------------------------------------------------

resource builderPushes 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, '${environment}-builder', acrPush)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPush)
    principalId: builder.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

resource builderReadsRegistry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, '${environment}-builder', reader)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
    principalId: builder.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// outputs.json comes from the newest deployment of the group.
resource readerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, environment, 'delivery-reader')
  properties: {
    roleName: '${environment} delivery reader (${resourceGroup().name})'
    description: 'Northwind course: read the platform deployment outputs and the alerts that gate a canary'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [
      {
        actions: [
          'Microsoft.Resources/subscriptions/resourceGroups/read'
          'Microsoft.Resources/deployments/read'
          'Microsoft.Insights/metricAlerts/read'
          'Microsoft.AlertsManagement/alerts/read'
          'Microsoft.App/containerApps/read'
          'Microsoft.App/managedEnvironments/read'
          'Microsoft.ContainerRegistry/registries/read'
        ]
        notActions: []
      }
    ]
  }
}

resource builderReads 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, '${environment}-builder', readerRole.id)
  properties: {
    roleDefinitionId: readerRole.id
    principalId: builder.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// ----- the deployer ---------------------------------------------------------------------------

resource deployerPushes 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, '${environment}-deployer', acrPush)
  scope: registry
  properties: {
    // Resolving a tag to its digest reads the registry's data plane.
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPush)
    principalId: deployer.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

resource deployerReads 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, '${environment}-deployer', readerRole.id)
  properties: {
    roleDefinitionId: readerRole.id
    principalId: deployer.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// What a release does to a live app: a new revision, the traffic split, a label, a tag.
resource deployerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, environment, 'deployer')
  properties: {
    roleName: '${environment} deployer (${resourceGroup().name})'
    description: 'Northwind course: roll a new image into a live Container App with a traffic split; assigned on the live apps, the live identity and the apps environment only'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [
      {
        actions: [
          'Microsoft.App/containerApps/*'
          'Microsoft.App/managedEnvironments/read'
          'Microsoft.App/managedEnvironments/join/action'
          'Microsoft.ManagedIdentity/userAssignedIdentities/read'
          'Microsoft.ManagedIdentity/userAssignedIdentities/assign/action'
          'Microsoft.Resources/tags/*'
        ]
        notActions: [
          'Microsoft.App/containerApps/delete'
        ]
      }
    ]
  }
}

resource deploysLiveApps 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (n, i) in liveAppNames: {
    name: guid(liveApps[i].id, '${environment}-deployer', deployerRole.id)
    scope: liveApps[i]
    properties: {
      roleDefinitionId: deployerRole.id
      principalId: deployer.outputs.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource assignsLiveIdentity 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(liveIdentity.id, '${environment}-deployer', deployerRole.id)
  scope: liveIdentity
  properties: {
    roleDefinitionId: deployerRole.id
    principalId: deployer.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

resource joinsEnvironment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appsEnvironment.id, '${environment}-deployer', deployerRole.id)
  scope: appsEnvironment
  properties: {
    roleDefinitionId: deployerRole.id
    principalId: deployer.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// The model promotion into the live endpoints: the endpoint role there, and reading the
// workspace for the model version and environment it deploys.
resource operatesLiveEndpoints 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (n, i) in liveEndpointNames: {
    name: guid(liveEndpoints[i].id, '${environment}-deployer', endpointRoleId)
    scope: liveEndpoints[i]
    properties: {
      roleDefinitionId: endpointRoleId
      principalId: deployer.outputs.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource readsWorkspace 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(ml.id, '${environment}-deployer', reader)
  scope: ml
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
    principalId: deployer.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

output deployerName string = deployer.outputs.name
output deployerClientId string = deployer.outputs.clientId
output deployerPrincipalId string = deployer.outputs.principalId
output builderClientId string = builder.outputs.clientId
output signingKeyName string = signingKey.name
