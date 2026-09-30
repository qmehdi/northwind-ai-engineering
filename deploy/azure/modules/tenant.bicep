// One owner of the platform: a tenant (a learner's namespace) or `live` (the promoted target).
// Everything here is scoped to the owner and named `<environment>-<owner>-<kind>`:
//
//   online endpoints  `nw-<owner>-<kind>-<scope>` for triage and semantic: endpoint names are
//                     unique per region across all customers and 32 characters at most, so they
//                     take the short form nw/platform/azure.py computes (`endpoint_name`), where
//                     `<scope>` is five hex characters of SHA-256 of subscription, resource
//                     group and environment
//   retraining        `<environment>-<owner>-retrain-triage`, an Azure ML schedule, disabled
//                     until `retrainEnabled`; tenants only
//   roles             least privilege for the owner's identity (and the learner, when
//                     `userObjectId` is given): read `data` and `baselines`, write only under
//                     `<environment>-<owner>/` in `artifacts` and `pipelines` (ABAC on the blob
//                     path), the tenant role on the workspace, the endpoint role on its own
//                     endpoints only (never the live ones: the live identity, the platform
//                     owner and the deployer hold it there), AcrPull, Key Vault Secrets User
//                     on its three secrets only (its API key map, its gateway key, the
//                     Application Insights connection string), and the tenant project role
//                     on the Foundry project (agents and evaluations, no model inference: models
//                     are reached through the gateway).
//
// The search index `<environment>-<owner>-policies` and its role are created by
// scripts/deploy_azure.sh: indexes are data plane.
metadata owner = 'northwind'

param environment string
param location string
param tags object
param owner string
param endpointKinds array
param endpointScope string
param principalId string
param identityId string
param identityClientId string
param userObjectId string
param lakeName string
param workspaceStorageName string
param keyVaultName string
param acrName string
param workspaceName string
param tenantWorkspaceRoleId string
param tenantEndpointRoleId string
param tenantProjectRoleId string
param foundryAccountName string
param foundryProjectName string
param clusterId string
param pipelinesEnvironmentId string
param retrainEnabled bool
param retrainCron string
param endpointTraffic object
param jobEnv object

var isLive = owner == 'live'
var prefix = '${environment}-${owner}'
var sourceUri = 'azureml://datastores/${replace(environment, '-', '_')}_artifacts/paths/${prefix}/source/latest.tar.gz'
var principals = concat(
  [{ id: principalId, type: 'ServicePrincipal' }],
  empty(userObjectId) ? [] : [{ id: userObjectId, type: 'User' }]
)
var roles = {
  blobReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  blobContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  secretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
}

// Read, write, delete, move and superuser access to blobs pass only under the owner's prefix
// (or `agents/`, where the shared agent registry document lives), and listing only with those
// prefixes (storage-auth-abac-examples, 2026-09-29).
var blobs = 'Microsoft.Storage/storageAccounts/blobServices/containers/blobs'
var prefixCondition = '((!(ActionMatches{\'${blobs}/read\'} AND NOT SubOperationMatches{\'Blob.List\'}) AND !(ActionMatches{\'${blobs}/write\'}) AND !(ActionMatches{\'${blobs}/add/action\'}) AND !(ActionMatches{\'${blobs}/delete\'}) AND !(ActionMatches{\'${blobs}/move/action\'}) AND !(ActionMatches{\'${blobs}/runAsSuperUser/action\'})) OR (@Resource[${blobs}:path] StringStartsWith \'${prefix}/\') OR (@Resource[${blobs}:path] StringStartsWith \'agents/\')) AND ((!(ActionMatches{\'${blobs}/read\'} AND SubOperationMatches{\'Blob.List\'})) OR (@Request[${blobs}:prefix] StringStartsWith \'${prefix}/\') OR (@Request[${blobs}:prefix] StringStartsWith \'agents/\'))'

// The owner's identity runs the apps and jobs (the policy service, the agent, the MCP server,
// the pipelines): the same prefix, except `<prefix>/approvals/`. That is where the ops store
// (nw/agent/opstore.py, NW_OPS_STORE) keeps claim markers, approval records and the escalation
// queue, and only the owner's approver writes it: the learner for a tenant, the platform owner
// (admin.bicep) for `live`. The runtimes write `trajectories/` (with the proposals) and
// `feedback/`; they never read `approvals/`, because the escalate tool's body runs only in the
// approver's process (nw/agent/approve.py). The data-plane half of ADR 0005.
var runtimeCondition = '((!(ActionMatches{\'${blobs}/read\'} AND NOT SubOperationMatches{\'Blob.List\'}) AND !(ActionMatches{\'${blobs}/write\'}) AND !(ActionMatches{\'${blobs}/add/action\'}) AND !(ActionMatches{\'${blobs}/delete\'}) AND !(ActionMatches{\'${blobs}/move/action\'}) AND !(ActionMatches{\'${blobs}/runAsSuperUser/action\'})) OR ((@Resource[${blobs}:path] StringStartsWith \'${prefix}/\') AND (@Resource[${blobs}:path] StringNotStartsWith \'${prefix}/approvals/\')) OR (@Resource[${blobs}:path] StringStartsWith \'agents/\')) AND ((!(ActionMatches{\'${blobs}/read\'} AND SubOperationMatches{\'Blob.List\'})) OR (@Request[${blobs}:prefix] StringStartsWith \'${prefix}/\') OR (@Request[${blobs}:prefix] StringStartsWith \'agents/\'))'

resource lake 'Microsoft.Storage/storageAccounts@2026-04-01' existing = {
  name: lakeName
}

resource lakeBlobs 'Microsoft.Storage/storageAccounts/blobServices@2026-04-01' existing = {
  parent: lake
  name: 'default'
}

resource readContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-04-01' existing = [
  for c in ['data', 'baselines']: {
    parent: lakeBlobs
    name: c
  }
]

resource writeContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-04-01' existing = [
  for c in ['artifacts', 'pipelines']: {
    parent: lakeBlobs
    name: c
  }
]

resource workspaceStorage 'Microsoft.Storage/storageAccounts@2026-04-01' existing = {
  name: workspaceStorageName
}

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' existing = {
  name: keyVaultName
}

// The owner's own x-api-key map: another owner's apps do not accept it.
resource apiKeySecret 'Microsoft.KeyVault/vaults/secrets@2026-02-01' existing = {
  parent: vault
  name: '${prefix}-api-key'
}

resource appInsightsSecret 'Microsoft.KeyVault/vaults/secrets@2026-02-01' existing = {
  parent: vault
  name: '${environment}-appinsights'
}

resource gatewayKeySecret 'Microsoft.KeyVault/vaults/secrets@2026-02-01' existing = {
  parent: vault
  name: '${prefix}-gateway-key'
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: acrName
}

resource ml 'Microsoft.MachineLearningServices/workspaces@2026-05-01' existing = {
  name: workspaceName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2026-07-01' existing = {
  name: foundryAccountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-07-01' existing = {
  parent: foundry
  name: foundryProjectName
}

// ----- the owner's online endpoints -----------------------------------------------------------

// Deployments are not declared here: a tenant's deployment appears when it approves a model
// version, and the live endpoint's blue and green deployments (data collection on, see
// deploy/azure/ml/deployment.yml) are created by the promotion drill. `endpointTraffic` carries
// the split the drill set, so a redeploy does not reset it.
resource endpoints 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints@2026-05-01' = [
  for kind in endpointKinds: {
    parent: ml
    name: 'nw-${owner}-${kind}-${endpointScope}'
    location: location
    tags: union(tags, { 'nw-tenant': owner, project: kind })
    kind: 'Managed'
    identity: {
      type: 'SystemAssigned'
    }
    properties: union(
      {
        // Key auth: nw/platform/azure.py reads the key with the endpoint role's listKeys.
        authMode: 'Key'
        publicNetworkAccess: 'Enabled'
        description: isLive
          ? 'Live ${kind}: blue and green deployments carry the canary'
          : 'Tenant ${owner}: the approved version of ${kind}'
      },
      contains(endpointTraffic, 'nw-${owner}-${kind}-${endpointScope}')
        ? { traffic: endpointTraffic['nw-${owner}-${kind}-${endpointScope}'] }
        : {}
    )
  }
]

// ----- the weekly retraining schedule ----------------------------------------------------------

// The job runs `nw.pipelines.retrain` in the pipelines image as the tenant's identity; it
// submits the tenant's Azure ML pipeline, whose gate decides. Disabled unless retrainEnabled.
resource retrain 'Microsoft.MachineLearningServices/workspaces/schedules@2026-05-01' = if (!isLive) {
  parent: ml
  name: '${prefix}-retrain-triage'
  properties: {
    displayName: '${prefix} weekly triage retraining'
    description: 'Weekly triage candidate for ${prefix}; the pipeline gate decides, never this schedule'
    isEnabled: retrainEnabled
    tags: {
      'nw-tenant': owner
      trigger: 'schedule'
    }
    trigger: {
      triggerType: 'Cron'
      expression: retrainCron
      timeZone: 'UTC'
    }
    action: {
      actionType: 'CreateJob'
      jobDefinition: {
        jobType: 'Command'
        displayName: '${prefix}-retrain-triage'
        experimentName: '${prefix}-triage'
        // The tenant's code, not the image's: the bundle of its last submission
        // (nw/pipelines/source.py), kept at <prefix>/source/latest.tar.gz in the artifacts
        // container and read through the lake's datastore.
        command: 'python -m nw.pipelines.retrain --pipeline triage --tenant ${owner} --trigger schedule --source-uri ${sourceUri}'
        environmentId: pipelinesEnvironmentId
        computeId: clusterId
        environmentVariables: union(jobEnv, {
          NW_TENANT: owner
          AZURE_CLIENT_ID: identityClientId
          NW_AZURE_PIPELINE_IDENTITY_CLIENT_ID: identityClientId
        })
        identity: {
          identityType: 'Managed'
          clientId: identityClientId
          resourceId: identityId
        }
      }
    }
  }
}

// ----- roles --------------------------------------------------------------------------------------

resource readsData 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for pair in flatten(map(range(0, 2), c => map(principals, p => { c: c, p: p }))): {
    name: guid(readContainers[pair.c].id, pair.p.id, roles.blobReader)
    scope: readContainers[pair.c]
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobReader)
      principalId: pair.p.id
      principalType: pair.p.type
    }
  }
]

resource writesOwnPrefix 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for pair in flatten(map(range(0, 2), c => map(principals, p => { c: c, p: p }))): {
    name: guid(writeContainers[pair.c].id, pair.p.id, roles.blobContributor)
    scope: writeContainers[pair.c]
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobContributor)
      principalId: pair.p.id
      principalType: pair.p.type
      description: pair.p.type == 'User' ? 'Only under ${prefix}/' : 'Only under ${prefix}/, not ${prefix}/approvals/'
      condition: pair.p.type == 'User' ? prefixCondition : runtimeCondition
      conditionVersion: '2.0'
    }
  }
]

// Job snapshots and outputs in the workspace's own storage (shared by design of Azure ML).
resource writesJobStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in principals: if (!isLive) {
    name: guid(workspaceStorage.id, p.id, roles.blobContributor)
    scope: workspaceStorage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobContributor)
      principalId: p.id
      principalType: p.type
    }
  }
]

resource readsApiKey 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in principals: {
    name: guid(apiKeySecret.id, p.id, roles.secretsUser)
    scope: apiKeySecret
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.secretsUser)
      principalId: p.id
      principalType: p.type
    }
  }
]

resource readsAppInsights 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in principals: {
    name: guid(appInsightsSecret.id, p.id, roles.secretsUser)
    scope: appInsightsSecret
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.secretsUser)
      principalId: p.id
      principalType: p.type
    }
  }
]

resource readsGatewayKey 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in principals: {
    name: guid(gatewayKeySecret.id, p.id, roles.secretsUser)
    scope: gatewayKeySecret
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.secretsUser)
      principalId: p.id
      principalType: p.type
    }
  }
]

resource pullsImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in principals: {
    name: guid(registry.id, p.id, roles.acrPull)
    scope: registry
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
      principalId: p.id
      principalType: p.type
    }
  }
]

resource usesWorkspace 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in principals: if (!isLive) {
    name: guid(ml.id, p.id, tenantWorkspaceRoleId)
    scope: ml
    properties: {
      roleDefinitionId: tenantWorkspaceRoleId
      principalId: p.id
      principalType: p.type
    }
  }
]

resource operatesEndpoint 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for pair in flatten(map(range(0, length(endpointKinds)), e => map(principals, p => { e: e, p: p }))): {
    name: guid(endpoints[pair.e].id, pair.p.id, tenantEndpointRoleId)
    scope: endpoints[pair.e]
    properties: {
      roleDefinitionId: tenantEndpointRoleId
      principalId: pair.p.id
      principalType: pair.p.type
    }
  }
]

// Agents and evaluations in the shared project, no inference data actions (foundry.bicep).
resource usesProject 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for p in principals: {
    name: guid(project.id, p.id, tenantProjectRoleId)
    scope: project
    properties: {
      roleDefinitionId: tenantProjectRoleId
      principalId: p.id
      principalType: p.type
    }
  }
]

output owner string = owner
output endpointNames array = [for (k, i) in endpointKinds: endpoints[i].name]
output scheduleName string = isLive ? '' : retrain.name
output prefix string = prefix
