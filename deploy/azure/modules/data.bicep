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
output lakeId string = lake.outputs.resourceId
output lakeDfsEndpoint string = 'https://${lake.outputs.name}.dfs.${az.environment().suffixes.storage}'
output workspaceStorageId string = workspaceStorage.outputs.resourceId
output workspaceStorageName string = workspaceStorage.outputs.name
output containers array = containers
output purviewName string = purview ? purviewAccount.name : ''
