// Serving: the alerts the promotion drill watches. The online endpoints themselves are per
// owner (tenant.bicep): one per tenant for learning, one live endpoint whose blue and green
// deployments carry the canary. A canary is rolled back when one of these fires:
//
//   <environment>-live-5xx       server errors on the live endpoint (RequestsPerMinute,
//                                statusCodeClass 5xx)
//   <environment>-live-p95       p95 latency on the live endpoint above `latencyP95Ms`
//   <environment>-live-<app>-5xx server errors on the live policy and agent apps (the image canary)
//
// Metric names from the supported metrics pages of Microsoft.MachineLearningServices/workspaces/
// onlineEndpoints and Microsoft.App/containerApps (learn.microsoft.com, 2026-09-29).
metadata owner = 'northwind'

param environment string
param tags object
param liveEndpointId string
param liveAppIds array
param actionGroupId string
param latencyP95Ms int
param enableTelemetry bool

module endpoint5xx 'br/public:avm/res/insights/metric-alert:0.4.1' = {
  name: '${environment}-live-5xx'
  params: {
    name: '${environment}-live-5xx'
    alertDescription: 'Server errors on the live online endpoint: roll the canary back (set traffic to the stable deployment).'
    location: 'global'
    tags: tags
    severity: 1
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    scopes: [liveEndpointId]
    targetResourceType: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints'
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allof: [
        {
          name: 'server-errors'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints'
          metricName: 'RequestsPerMinute'
          dimensions: [
            {
              name: 'statusCodeClass'
              operator: 'Include'
              values: ['5xx']
            }
          ]
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Total'
        }
      ]
    }
    actions: [actionGroupId]
    enableTelemetry: enableTelemetry
  }
}

module endpointP95 'br/public:avm/res/insights/metric-alert:0.4.1' = {
  name: '${environment}-live-p95'
  params: {
    name: '${environment}-live-p95'
    alertDescription: 'p95 latency on the live online endpoint above ${latencyP95Ms} ms: hold or roll back the canary.'
    location: 'global'
    tags: tags
    severity: 2
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    scopes: [liveEndpointId]
    targetResourceType: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints'
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allof: [
        {
          name: 'p95-latency'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints'
          metricName: 'RequestLatency_P95'
          operator: 'GreaterThan'
          threshold: latencyP95Ms
          timeAggregation: 'Average'
        }
      ]
    }
    actions: [actionGroupId]
    enableTelemetry: enableTelemetry
  }
}

module app5xx 'br/public:avm/res/insights/metric-alert:0.4.1' = [
  for a in liveAppIds: {
    name: '${a.name}-5xx'
    params: {
      name: '${a.name}-5xx'
      alertDescription: 'Server errors on ${a.name}: `make approve-azure` must not run; the release script sets the stable revision back to 100.'
      location: 'global'
      tags: tags
      severity: 1
      evaluationFrequency: 'PT1M'
      windowSize: 'PT5M'
      scopes: [a.id]
      targetResourceType: 'Microsoft.App/containerApps'
      autoMitigate: true
      criteria: {
        'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
        allof: [
          {
            name: 'server-errors'
            criterionType: 'StaticThresholdCriterion'
            metricNamespace: 'Microsoft.App/containerApps'
            metricName: 'Requests'
            dimensions: [
              {
                name: 'statusCodeCategory'
                operator: 'Include'
                values: ['5xx']
              }
            ]
            operator: 'GreaterThan'
            threshold: 0
            timeAggregation: 'Total'
          }
        ]
      }
      actions: [actionGroupId]
      enableTelemetry: enableTelemetry
    }
  }
]

output alertNames array = concat(
  ['${environment}-live-5xx', '${environment}-live-p95'],
  map(liveAppIds, a => '${a.name}-5xx')
)
