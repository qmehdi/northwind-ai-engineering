// The allowed-sizes policy definition, at subscription scope because Azure Policy has no
// resource group definitions; main.bicep assigns it to the platform's resource group only. It
// denies an Azure Machine Learning compute (cluster or instance) or a managed online
// deployment whose size is not on the list, and an online deployment with more instances
// than `maxInstances`: a tenant cannot start a GPU box by typing its name.
//
// Aliases from the built-in "Limit allowed vm sizes for Azure Machine Learning compute" (Azure
// Landing Zones, Deny-MachineLearning-Compute-VmSize) and the provider's alias list for
// workspaces/onlineEndpoints/deployments (instanceType, sku.capacity), checked 2026-09-30.
// Serverless jobs name their instance type inside the job, which is not an ARM property the
// policy sees; the workspace's vCPU quota per family bounds them (deploy/azure/README.md).
// `make destroy-azure` deletes the definition.
metadata owner = 'northwind'
targetScope = 'subscription'

param environment string
param resourceGroupName string

resource sizes 'Microsoft.Authorization/policyDefinitions@2025-01-01' = {
  name: guid(subscription().id, resourceGroupName, environment, 'ml-sizes')
  properties: {
    displayName: '${environment} allowed Azure ML sizes (${resourceGroupName})'
    description: 'Northwind course: Azure ML computes and managed online deployments only in the listed sizes, online deployments with at most maxInstances instances'
    policyType: 'Custom'
    mode: 'All'
    metadata: {
      category: 'Machine Learning'
    }
    parameters: {
      allowedSizes: {
        type: 'Array'
        metadata: {
          displayName: 'Allowed VM sizes'
        }
      }
      maxInstances: {
        type: 'Integer'
        metadata: {
          displayName: 'Most instances per online deployment'
        }
      }
    }
    policyRule: {
      if: {
        anyOf: [
          {
            allOf: [
              {
                field: 'type'
                equals: 'Microsoft.MachineLearningServices/workspaces/computes'
              }
              {
                field: 'Microsoft.MachineLearningServices/workspaces/computes/computeType'
                in: ['AmlCompute', 'ComputeInstance']
              }
              {
                field: 'Microsoft.MachineLearningServices/workspaces/computes/vmSize'
                notIn: '[parameters(\'allowedSizes\')]'
              }
            ]
          }
          {
            allOf: [
              {
                field: 'type'
                equals: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments'
              }
              {
                anyOf: [
                  {
                    field: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments/instanceType'
                    notIn: '[parameters(\'allowedSizes\')]'
                  }
                  {
                    field: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments/sku.capacity'
                    greater: '[parameters(\'maxInstances\')]'
                  }
                ]
              }
            ]
          }
        ]
      }
      then: {
        effect: 'deny'
      }
    }
  }
}

output definitionId string = sizes.id
