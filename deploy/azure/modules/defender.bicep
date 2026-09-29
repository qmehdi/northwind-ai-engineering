// Microsoft Defender for Containers at subscription scope: vulnerability scanning of every image
// pushed to the registry (and runtime protection where it applies). Optional and off: it is a
// subscription-wide plan, billed per vCore-hour, and an organisation turns it on centrally.
metadata owner = 'northwind'
targetScope = 'subscription'

resource containers 'Microsoft.Security/pricings@2024-01-01' = {
  name: 'Containers'
  properties: {
    pricingTier: 'Standard'
  }
}

output plan string = containers.properties.pricingTier
