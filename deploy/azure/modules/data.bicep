// Data and governance: the data lake (Storage with hierarchical namespace) that holds the
// tickets, every tenant's artifacts and pipeline outputs, and the two production summaries the
// promotion gates read; Microsoft Purview is optional and off. A tenant writes only under its
// own prefix (`<environment>-<tenant>/`): the role assignments in tenant.bicep carry an ABAC
// condition on the blob path. The production summaries are uploaded by
// scripts/deploy_azure.sh after the deployment (blob contents are data plane, not ARM).
metadata owner = 'northwind'

param environment string
param location string
param tags object
param suffix string
param purview bool
@description('Every owner (tenants and live): the retention rules are per owner prefix.')
param owners array
@description('Days before operational blobs (captures, trajectories, feedback, monitoring output) are deleted; docs/governance retention table: 90.')
param captureRetentionDays int = 90
@description('Days before audit blobs (stage trails, approvals) are deleted; docs/governance retention table: 400.')
param auditRetentionDays int = 400
param logsWorkspaceId string
param enableTelemetry bool

// The four containers of the platform, one name each (the brief's data, artifacts,
// pipelines and baselines).
var containers = ['data', 'artifacts', 'pipelines', 'baselines']

module lake 'br/public:avm/res/storage/storage-account:0.33.1' = {
  name: '${environment}-lake'
  params: {
    // `nw<kind><suffix>`: Storage names are 3 to 24 lowercase letters and digits, globally unique.
    name: 'nwdata${suffix}'
    location: location
    tags: tags
    kind: 'StorageV2'
    skuName: 'Standard_LRS'
    accessTier: 'Hot'
    enableHierarchicalNamespace: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    blobServices: {
      containers: [
        for c in containers: {
          name: c
          publicAccess: 'None'
        }
      ]
      // Soft delete keeps a deleted artifact for a week, which is the promotion drill's undo.
      deleteRetentionPolicyEnabled: true
      deleteRetentionPolicyDays: 7
      containerDeleteRetentionPolicyEnabled: true
      containerDeleteRetentionPolicyDays: 7
    }
    diagnosticSettings: [
      {
        name: 'to-logs'
        workspaceResourceId: logsWorkspaceId
        metricCategories: [
          {
            category: 'Transaction'
          }
        ]
      }
    ]
    enableTelemetry: enableTelemetry
  }
}

// Retention (deploy/azure/README.md "Retention"): lifecycle rules per owner prefix, because a
// rule's prefix starts with a container name and cannot wildcard the owner.
//   operational         artifacts/<environment>-<owner>/capture/, /traces/, /trajectories/,
//                       /feedback/ and /monitoring/ (captured requests, agent trajectories and
//                       feedback from the ops store, monitoring and drift output): deleted
//                       `captureRetentionDays` (90) after the last write
//   audit               artifacts/<environment>-<owner>/registry/ (the stage trail of every
//                       model version), /audit/ and /approvals/ (the ops store's claim markers,
//                       approval records and escalation queue): deleted `auditRetentionDays`
//                       (400) after the last write
// The live endpoints' data collector writes to the workspace storage instead; tracking.bicep
// holds that rule. Rule names are letters and digits only.
var ruleEnv = replace(environment, '-', '')
var retentionRules = flatten(map(owners, o => [
  {
    name: 'capture${ruleEnv}${o}'
    enabled: true
    type: 'Lifecycle'
    definition: {
      filters: {
        blobTypes: ['blockBlob', 'appendBlob']
        prefixMatch: [
          'artifacts/${environment}-${o}/capture/'
          'artifacts/${environment}-${o}/traces/'
          'artifacts/${environment}-${o}/trajectories/'
          'artifacts/${environment}-${o}/feedback/'
          'artifacts/${environment}-${o}/monitoring/'
        ]
      }
      actions: {
        baseBlob: {
          delete: {
            daysAfterModificationGreaterThan: captureRetentionDays
          }
        }
      }
    }
  }
  {
    name: 'audit${ruleEnv}${o}'
    enabled: true
    type: 'Lifecycle'
    definition: {
      filters: {
        blobTypes: ['blockBlob', 'appendBlob']
        prefixMatch: [
          'artifacts/${environment}-${o}/registry/'
          'artifacts/${environment}-${o}/audit/'
          'artifacts/${environment}-${o}/approvals/'
        ]
      }
      actions: {
        baseBlob: {
          delete: {
            daysAfterModificationGreaterThan: auditRetentionDays
          }
        }
      }
    }
  }
]))

resource lakeAccount 'Microsoft.Storage/storageAccounts@2026-04-01' existing = {
  name: 'nwdata${suffix}'
  dependsOn: [lake]
}

resource lakeRetention 'Microsoft.Storage/storageAccounts/managementPolicies@2026-04-01' = {
  parent: lakeAccount
  name: 'default'
  properties: {
    policy: {
      rules: retentionRules
    }
  }
}

// Azure Machine Learning does not accept a hierarchical namespace account as the workspace's
// own storage, so the workspace gets a second, flat account for its job snapshots and outputs.
module workspaceStorage 'br/public:avm/res/storage/storage-account:0.33.1' = {
  name: '${environment}-mlstore'
  params: {
    name: 'nwml${suffix}'
    location: location
    tags: tags
    kind: 'StorageV2'
    skuName: 'Standard_LRS'
    accessTier: 'Hot'
    enableHierarchicalNamespace: false
    allowBlobPublicAccess: false
    // The workspace's system datastores authenticate with identity (tracking.bicep); shared key
    // stays on because Azure Machine Learning studio's file browser still uses it.
    allowSharedKeyAccess: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    enableTelemetry: enableTelemetry
  }
}

// Microsoft Purview: optional, off by default. It scans the lake into the Data Map when an
// organisation wants lineage and classification; the course leaves it to the reference tab.
resource purviewAccount 'Microsoft.Purview/accounts@2021-12-01' = if (purview) {
  name: '${environment}-purview-${suffix}'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publicNetworkAccess: 'Enabled'
  }
}

output lakeName string = lake.outputs.name
// NW_OPS_STORE: the ops store's keys land in artifacts/<environment>-<owner>/<kind>/.
output opsStore string = 'https://${lake.outputs.name}.blob.${az.environment().suffixes.storage}/artifacts'
output lakeId string = lake.outputs.resourceId
output lakeDfsEndpoint string = 'https://${lake.outputs.name}.dfs.${az.environment().suffixes.storage}'
output workspaceStorageId string = workspaceStorage.outputs.resourceId
output workspaceStorageName string = workspaceStorage.outputs.name
output containers array = containers
output purviewName string = purview ? purviewAccount.name : ''
