// Delivery: the identity the delivery pipeline deploys as, and what it may touch. Azure
// Pipelines (deploy/azure/pipelines/azure-pipelines.yml) signs in through a workload identity
// federation service connection bound to this identity; GitHub Actions with OIDC
// (deploy/azure/pipelines/github-actions.yml) is the documented alternative. Either way there is
// no secret: the federated credential trusts one issuer and one subject.
//
// The deployer pushes images (AcrPush), updates the live Container Apps and moves their traffic
// (a custom role on the resource group: container apps, joining the environment, assigning the
// live identity), and reads alerts, which is what the canary gate checks. The manual approval is
// an Azure Pipelines environment check (or a GitHub environment rule): those live in the
// DevOps project, not in ARM, and deploy/azure/README.md says where to click.
metadata owner = 'northwind'

param environment string
param location string
param tags object
param acrName string
param githubRepository string
param azureDevOpsIssuer string
param azureDevOpsSubject string
param enableTelemetry bool

var acrPush = '8311e382-0749-4cb8-b61a-304f252e45ec'
var federated = concat(
  empty(githubRepository)
    ? []
    : [
        {
          name: 'github-${environment}-live'
          issuer: 'https://token.actions.githubusercontent.com'
          subject: 'repo:${githubRepository}:environment:${environment}-live'
          audiences: ['api://AzureADTokenExchange']
        }
        {
          name: 'github-main'
          issuer: 'https://token.actions.githubusercontent.com'
          subject: 'repo:${githubRepository}:ref:refs/heads/main'
          audiences: ['api://AzureADTokenExchange']
        }
      ],
  empty(azureDevOpsIssuer) || empty(azureDevOpsSubject)
    ? []
    : [
        {
          name: 'azure-pipelines'
          issuer: azureDevOpsIssuer
          subject: azureDevOpsSubject
          audiences: ['api://AzureADTokenExchange']
        }
      ]
)

module deployer 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: '${environment}-deployer'
  params: {
    name: '${environment}-deployer'
    location: location
    tags: tags
    federatedIdentityCredentials: federated
    enableTelemetry: enableTelemetry
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: acrName
}

resource pushesImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, '${environment}-deployer', acrPush)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPush)
    principalId: deployer.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

resource deployerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, environment, 'deployer')
  properties: {
    roleName: '${environment} deployer (${resourceGroup().name})'
    description: 'Northwind course: roll a new image into the live Container Apps with a traffic split, read the alerts that gate it'
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
          'Microsoft.ContainerRegistry/registries/read'
          'Microsoft.Insights/metricAlerts/read'
          'Microsoft.AlertsManagement/alerts/read'
          'Microsoft.Resources/subscriptions/resourceGroups/read'
          'Microsoft.Resources/deployments/read'
          'Microsoft.Resources/tags/*'
        ]
        notActions: [
          'Microsoft.App/containerApps/delete'
        ]
      }
    ]
  }
}

resource deploys 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, '${environment}-deployer', deployerRole.id)
  properties: {
    roleDefinitionId: deployerRole.id
    principalId: deployer.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

output deployerName string = deployer.outputs.name
output deployerClientId string = deployer.outputs.clientId
output deployerPrincipalId string = deployer.outputs.principalId
