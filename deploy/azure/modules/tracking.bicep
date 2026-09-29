// Tracking and registry: the Azure Machine Learning workspace (experiments, the model
// registry, pipelines, online endpoints) with its Key Vault, Application Insights, storage and
// container registry; the training cluster that scales to zero; the datastores over the lake;
// the pipelines environment; one user-assigned managed identity per tenant; and the two
// custom roles that give a tenant the workspace without its neighbours' endpoints.
// Azure Machine Learning's finest RBAC scope for jobs and models is the workspace, so tenants
// share it and are kept apart by name (`northwind-<tenant>-triage`) and by the endpoint role,
// which is assigned on the tenant's own endpoint only (tenant.bicep).
metadata owner = 'northwind'

param environment string
param location string
param tags object
param suffix string
param tenants array
param acrSku string
param lakeName string
param lakeId string
param workspaceStorageId string
param appInsightsId string
param logsWorkspaceId string
param pipelinesImage string
param pipelinesImageVersion string
@secure()
param apiKey string
param enableTelemetry bool

var storageBlobDataReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

module vault 'br/public:avm/res/key-vault/vault:0.14.2' = {
  name: '${environment}-vault'
  params: {
    // `nw<kind><suffix>`: Key Vault names are 3 to 24 characters and globally unique.
    name: 'nwkv${suffix}'
    location: location
    tags: tags
    sku: 'standard'
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    // Purge protection is irreversible and would hold the name for the retention period after
    // `make destroy-azure`; the course turns it off, an organisation turns it on.
    enablePurgeProtection: false
    publicNetworkAccess: 'Enabled'
    secrets: [
      {
        name: '${environment}-api-key'
        value: apiKey
        contentType: 'The cohort x-api-key every service checks (nw/auth.py); scripts/rotate_key.sh adds versions'
      }
    ]
    diagnosticSettings: [
      {
        name: 'to-logs'
        workspaceResourceId: logsWorkspaceId
      }
    ]
    enableTelemetry: enableTelemetry
  }
}

module registry 'br/public:avm/res/container-registry/registry:0.13.1' = {
  name: '${environment}-registry'
  params: {
    // `nw<kind><suffix>`: registry names are 5 to 50 letters and digits, globally unique.
    name: 'nwacr${suffix}'
    location: location
    tags: tags
    acrSku: acrSku
    acrAdminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Enabled'
    diagnosticSettings: [
      {
        name: 'to-logs'
        workspaceResourceId: logsWorkspaceId
      }
    ]
    enableTelemetry: enableTelemetry
  }
}

// One identity per owner (each tenant, and `live` for the promoted services): its pipeline
// jobs, its Container Apps and its schedule run as it.
module identities 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = [
  for t in tenants: {
    name: '${environment}-${t}-identity'
    params: {
      name: '${environment}-${t}-id'
      location: location
      tags: union(tags, { 'nw-tenant': t })
      enableTelemetry: enableTelemetry
    }
  }
]

module workspace 'br/public:avm/res/machine-learning-services/workspace:0.14.1' = {
  name: '${environment}-ml'
  params: {
    name: '${environment}-ml'
    friendlyName: '${environment} platform'
    location: location
    tags: tags
    sku: 'Basic'
    kind: 'Default'
    associatedStorageAccountResourceId: workspaceStorageId
    associatedKeyVaultResourceId: vault.outputs.resourceId
    associatedApplicationInsightsResourceId: appInsightsId
    associatedContainerRegistryResourceId: registry.outputs.resourceId
    // Serverless jobs (how nw/platform/azure.py submits pipelines) run as a tenant identity only
    // when that identity is attached to the workspace.
    managedIdentities: {
      systemAssigned: true
      userAssignedResourceIds: [
        for t in tenants: resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', '${environment}-${t}-id')
      ]
    }
    systemDatastoresAuthMode: 'Identity'
    publicNetworkAccess: 'Enabled'
    // The lake's four containers as identity-based datastores: a job reads and writes them as
    // the identity it runs under, so the tenant's ABAC condition applies inside a pipeline too.
    datastores: [
      for c in ['data', 'artifacts', 'pipelines', 'baselines']: {
        name: '${replace(environment, '-', '_')}_${c}'
        properties: {
          datastoreType: 'AzureDataLakeGen2'
          accountName: lakeName
          filesystem: c
          endpoint: az.environment().suffixes.storage
          protocol: 'https'
          credentials: {
            credentialsType: 'None'
          }
          serviceDataAccessAuthIdentity: 'WorkspaceSystemAssignedIdentity'
          description: 'The ${c} container of the ${environment} lake'
        }
      }
    ]
    diagnosticSettings: [
      {
        name: 'to-logs'
        workspaceResourceId: logsWorkspaceId
      }
    ]
    enableTelemetry: enableTelemetry
  }
  dependsOn: [identities]
}

resource ml 'Microsoft.MachineLearningServices/workspaces@2026-05-01' existing = {
  name: '${environment}-ml'
  dependsOn: [workspace]
}

resource lake 'Microsoft.Storage/storageAccounts@2026-04-01' existing = {
  name: lakeName
}

// The studio previews datastore contents as the workspace identity.
resource workspaceReadsLake 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(lakeId, '${environment}-ml', storageBlobDataReader)
  scope: lake
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReader)
    principalId: workspace.outputs.systemAssignedMIPrincipalId!
    principalType: 'ServicePrincipal'
  }
}

// The training cluster: CPU, scales to zero after two idle minutes, at most four nodes. Every
// tenant identity is attached so a job can run as its tenant. Native because the identity list
// is per tenant and the Azure Verified Module takes computes as untyped objects anyway.
resource cluster 'Microsoft.MachineLearningServices/workspaces/computes@2026-05-01' = {
  parent: ml
  name: take('${environment}-train', 24)
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: toObject(
      tenants,
      t => resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', '${environment}-${t}-id'),
      t => {}
    )
  }
  properties: {
    computeType: 'AmlCompute'
    description: 'Pipeline steps for every tenant; zero nodes when idle'
    properties: {
      vmSize: 'Standard_DS3_v2'
      vmPriority: 'Dedicated'
      scaleSettings: {
        minNodeCount: 0
        maxNodeCount: 4
        nodeIdleTimeBeforeScaleDown: 'PT120S'
      }
      remoteLoginPortPublicAccess: 'Disabled'
      osType: 'Linux'
    }
  }
  dependsOn: [identities]
}

// The pipelines image as an environment, so a schedule (tenant.bicep) can name it. The version
// is the image tag: environment versions are immutable, a new tag is a new version.
resource pipelinesEnvironment 'Microsoft.MachineLearningServices/workspaces/environments@2026-05-01' = {
  parent: ml
  name: '${environment}-pipelines'
  properties: {
    description: 'The course pipelines image (Dockerfile, APP=pipelines), built by scripts/images_azure.sh'
  }
}

resource pipelinesEnvironmentVersion 'Microsoft.MachineLearningServices/workspaces/environments/versions@2026-05-01' = {
  parent: pipelinesEnvironment
  name: pipelinesImageVersion
  properties: {
    image: pipelinesImage
    osType: 'Linux'
    description: 'Image ${pipelinesImage}'
  }
}

// A tenant's rights on the shared workspace: everything a data scientist does (jobs, models,
// environments, components, data assets, experiments, reading endpoints and schedules) and
// nothing that touches the workspace itself, compute, datastores, connections, schedules or
// someone's endpoint. It is AzureML Data Scientist with more NotActions.
resource tenantWorkspaceRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, environment, 'tenant-workspace')
  properties: {
    roleName: '${environment} tenant on the workspace (${resourceGroup().name})'
    description: 'Northwind course: a tenant runs jobs and registers models in the shared Azure Machine Learning workspace'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [
      {
        actions: [
          'Microsoft.MachineLearningServices/workspaces/*/read'
          'Microsoft.MachineLearningServices/workspaces/*/action'
          'Microsoft.MachineLearningServices/workspaces/*/write'
          'Microsoft.MachineLearningServices/workspaces/*/delete'
          'Microsoft.Authorization/*/read'
        ]
        notActions: [
          'Microsoft.MachineLearningServices/workspaces/write'
          'Microsoft.MachineLearningServices/workspaces/delete'
          'Microsoft.MachineLearningServices/workspaces/listKeys/action'
          'Microsoft.MachineLearningServices/workspaces/computes/*/write'
          'Microsoft.MachineLearningServices/workspaces/computes/*/delete'
          'Microsoft.MachineLearningServices/workspaces/computes/listKeys/action'
          'Microsoft.MachineLearningServices/workspaces/datastores/write'
          'Microsoft.MachineLearningServices/workspaces/datastores/delete'
          'Microsoft.MachineLearningServices/workspaces/connections/*'
          'Microsoft.MachineLearningServices/workspaces/schedules/write'
          'Microsoft.MachineLearningServices/workspaces/schedules/delete'
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/write'
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/delete'
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments/write'
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments/delete'
        ]
      }
    ]
  }
}

// Assigned on one online endpoint: the tenant creates, updates and deletes deployments on it
// and moves its traffic. Nothing at workspace scope.
resource tenantEndpointRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, environment, 'tenant-endpoint')
  properties: {
    roleName: '${environment} endpoint operator (${resourceGroup().name})'
    description: 'Northwind course: deploy to and shift traffic on the one online endpoint this role is assigned on'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [
      {
        actions: [
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/read'
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/write'
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/token/action'
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/listKeys/action'
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/score/action'
          'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments/*'
        ]
        notActions: []
      }
    ]
  }
}

output keyVaultName string = vault.outputs.name
output keyVaultId string = vault.outputs.resourceId
output keyVaultUri string = vault.outputs.uri
output acrName string = registry.outputs.name
output acrId string = registry.outputs.resourceId
output acrLoginServer string = registry.outputs.loginServer
output workspaceName string = workspace.outputs.name
output workspaceId string = workspace.outputs.resourceId
output workspacePrincipalId string = workspace.outputs.systemAssignedMIPrincipalId!
output clusterName string = cluster.name
output clusterId string = cluster.id
output pipelinesEnvironmentId string = pipelinesEnvironmentVersion.id
output tenantWorkspaceRoleId string = tenantWorkspaceRole.id
output tenantEndpointRoleId string = tenantEndpointRole.id
output identityIds array = [for (t, i) in tenants: identities[i].outputs.resourceId]
output identityPrincipalIds array = [for (t, i) in tenants: identities[i].outputs.principalId]
output identityClientIds array = [for (t, i) in tenants: identities[i].outputs.clientId]
