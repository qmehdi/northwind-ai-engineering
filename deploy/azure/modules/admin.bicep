// The platform owner (whoever runs `make deploy-azure`, or the instructor in a cohort): the
// data plane rights an Owner of the resource group does not have by role. The deploy script
// uploads the tickets and the production summaries to the lake, creates the search indexes and
// writes gateway keys; those calls need these roles. It also holds the endpoint role on the
// live online endpoints: in a cohort the instructor runs the model promotion drill (tenants
// no longer hold that role there). Empty `adminObjectId` skips them.
metadata owner = 'northwind'

param environment string
param adminObjectId string
param adminPrincipalType string
param lakeName string
param searchName string
param keyVaultName string
param foundryAccountName string
param workspaceName string
param liveEndpointNames array
param endpointRoleId string

var roles = [
  { scope: 'lake', id: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe' } // Storage Blob Data Contributor
  { scope: 'search', id: '7ca78c08-252a-4471-8644-bb5ff32d4ba0' } // Search Service Contributor
  { scope: 'search', id: '8ebe5a00-799e-43f5-93ac-243d3dce84a7' } // Search Index Data Contributor
  { scope: 'vault', id: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7' } // Key Vault Secrets Officer
  { scope: 'foundry', id: '53ca6127-db72-4b80-b1b0-d745d6d5456d' } // Foundry User
]

resource lake 'Microsoft.Storage/storageAccounts@2026-04-01' existing = {
  name: lakeName
}

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchName
}

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' existing = {
  name: keyVaultName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2026-07-01' existing = {
  name: foundryAccountName
}

resource onLake 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in filter(roles, x => x.scope == 'lake'): if (!empty(adminObjectId)) {
    name: guid(lake.id, adminObjectId, r.id)
    scope: lake
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r.id)
      principalId: adminObjectId
      principalType: adminPrincipalType
      description: '${environment} platform owner'
    }
  }
]

resource onSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in filter(roles, x => x.scope == 'search'): if (!empty(adminObjectId)) {
    name: guid(search.id, adminObjectId, r.id)
    scope: search
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r.id)
      principalId: adminObjectId
      principalType: adminPrincipalType
      description: '${environment} platform owner'
    }
  }
]

resource onVault 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in filter(roles, x => x.scope == 'vault'): if (!empty(adminObjectId)) {
    name: guid(vault.id, adminObjectId, r.id)
    scope: vault
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r.id)
      principalId: adminObjectId
      principalType: adminPrincipalType
      description: '${environment} platform owner'
    }
  }
]

resource onFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in filter(roles, x => x.scope == 'foundry'): if (!empty(adminObjectId)) {
    name: guid(foundry.id, adminObjectId, r.id)
    scope: foundry
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r.id)
      principalId: adminObjectId
      principalType: adminPrincipalType
      description: '${environment} platform owner'
    }
  }
]

resource ml 'Microsoft.MachineLearningServices/workspaces@2026-05-01' existing = {
  name: workspaceName
}

resource liveEndpoints 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints@2026-05-01' existing = [
  for n in liveEndpointNames: {
    parent: ml
    name: n
  }
]

resource onLiveEndpoints 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (n, i) in liveEndpointNames: if (!empty(adminObjectId)) {
    name: guid(liveEndpoints[i].id, adminObjectId, endpointRoleId)
    scope: liveEndpoints[i]
    properties: {
      roleDefinitionId: endpointRoleId
      principalId: adminObjectId
      principalType: adminPrincipalType
      description: '${environment} platform owner: the live promotion drill'
    }
  }
]
