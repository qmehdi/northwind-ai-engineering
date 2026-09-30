// Network: the virtual network the Container Apps environment runs in, for two reasons.
//
//   egress   the apps' subnet carries a network security group whose outbound rules allow
//            the virtual network and Azure's service tags on 443 and deny the internet
//            (`egressControl`, on by default). An agent that is talked into fetching a URL or
//            posting data reaches Azure services only, not an arbitrary host. The limit, said
//            plainly: service tags name Azure services, not this platform's instances, so a
//            storage account or web app someone else owns in Azure is still reachable. Egress
//            by fully qualified name needs Azure Firewall with a route table on this subnet
//            (priced in deploy/COSTS-platform.md, not deployed).
//   gateway  in LiteLLM mode the gateway's PostgreSQL has no public endpoint: it sits in a
//            delegated subnet with a private DNS zone linked here, and only the apps of the
//            environment reach it (no `0.0.0.0` firewall rule).
//
// Outbound rules follow "Securing a virtual network in Azure Container Apps" (workload
// profiles, learn.microsoft.com, 2026-09-30): Microsoft Container Registry and its Front Door
// dependency, Entra ID for managed identities, Azure Monitor, the container registry, storage;
// AzureCloud on 443 covers the gateway (API Management), Key Vault, AI Search and the lake.
// Azure DNS (168.63.129.16) is never subject to the group.
metadata owner = 'northwind'

param environment string
param location string
param tags object
param egressControl bool
param postgres bool

var appsPrefix = '10.40.0.0/23'
var dbPrefix = '10.40.2.0/28'

var egressRules = [
  { name: 'vnet', priority: 100, protocol: '*', destination: 'VirtualNetwork', port: '*' }
  { name: 'mcr', priority: 110, protocol: 'Tcp', destination: 'MicrosoftContainerRegistry', port: '443' }
  { name: 'frontdoor', priority: 120, protocol: 'Tcp', destination: 'AzureFrontDoor.FirstParty', port: '443' }
  { name: 'entra', priority: 130, protocol: 'Tcp', destination: 'AzureActiveDirectory', port: '443' }
  { name: 'monitor', priority: 140, protocol: 'Tcp', destination: 'AzureMonitor', port: '443' }
  { name: 'acr', priority: 150, protocol: 'Tcp', destination: 'AzureContainerRegistry', port: '443' }
  { name: 'storage', priority: 160, protocol: 'Tcp', destination: 'Storage', port: '443' }
  { name: 'azure', priority: 170, protocol: 'Tcp', destination: 'AzureCloud', port: '443' }
]

resource nsg 'Microsoft.Network/networkSecurityGroups@2025-01-01' = {
  name: '${environment}-apps-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: egressControl
      ? concat(
          map(egressRules, r => {
            name: 'allow-${r.name}'
            properties: {
              priority: r.priority
              direction: 'Outbound'
              access: 'Allow'
              protocol: r.protocol
              sourceAddressPrefix: appsPrefix
              sourcePortRange: '*'
              destinationAddressPrefix: r.destination
              destinationPortRange: r.port
            }
          }),
          [
            {
              name: 'deny-internet'
              properties: {
                priority: 4000
                direction: 'Outbound'
                access: 'Deny'
                protocol: '*'
                sourceAddressPrefix: '*'
                sourcePortRange: '*'
                destinationAddressPrefix: 'Internet'
                destinationPortRange: '*'
              }
            }
          ]
        )
      : []
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2025-01-01' = {
  name: '${environment}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.40.0.0/16']
    }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: appsPrefix
          networkSecurityGroup: {
            id: nsg.id
          }
          delegations: [
            {
              name: 'container-apps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'gateway-db'
        properties: {
          addressPrefix: dbPrefix
          delegations: [
            {
              name: 'postgres'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

// The zone name must end in postgres.database.azure.com for a server in a delegated subnet.
resource dbZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (postgres) {
  name: '${environment}-gateway.private.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource dbZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (postgres) {
  parent: dbZone
  name: '${environment}-vnet-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

output vnetId string = vnet.id
output appsSubnetId string = '${vnet.id}/subnets/apps'
output dbSubnetId string = '${vnet.id}/subnets/gateway-db'
output dbZoneId string = postgres ? dbZone.id : ''
output egressControl bool = egressControl
