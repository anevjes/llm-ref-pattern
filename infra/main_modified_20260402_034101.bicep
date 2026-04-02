// ============================================================================
// Enterprise Hub-and-Spoke Infrastructure Template
// Provisions: Hub VNet, Spoke VNet, Azure Firewall, Bastion, NSGs,
//             App Service, SQL Server, Key Vault, Storage, Log Analytics,
//             Application Insights, RBAC, and Diagnostic Settings
// ============================================================================

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Environment name used in resource naming.')
@allowed(['dev', 'staging', 'prod'])
param environmentName string = 'dev'

@description('Project name prefix for all resources.')
@minLength(2)
@maxLength(12)
param projectName string = 'entlz'

@description('Address space for the hub virtual network.')
param hubVnetAddressSpace string = '10.0.0.0/16'

@description('Address prefix for the Azure Firewall subnet.')
param firewallSubnetPrefix string = '10.0.1.0/26'

@description('Address prefix for the Azure Bastion subnet.')
param bastionSubnetPrefix string = '10.0.2.0/26'

@description('Address prefix for the hub management subnet.')
param hubMgmtSubnetPrefix string = '10.0.3.0/24'

@description('Address space for the spoke virtual network.')
param spokeVnetAddressSpace string = '10.1.0.0/16'

@description('Address prefix for the spoke app subnet.')
param spokeAppSubnetPrefix string = '10.1.1.0/24'

@description('Address prefix for the spoke data subnet.')
param spokeDataSubnetPrefix string = '10.1.2.0/24'

@description('Address prefix for the spoke integration subnet.')
param spokeIntegrationSubnetPrefix string = '10.1.3.0/24'

@description('SKU tier for the App Service plan.')
@allowed(['S1', 'S2', 'S3', 'P1v3', 'P2v3', 'P3v3'])
param appServicePlanSku string = 'P1v3'

@description('SQL Server administrator login name.')
@secure()
param sqlAdminLogin string

@secure()
@description('SQL Server administrator login password.')
param sqlAdminPassword string

@description('SQL Database SKU name.')
@allowed(['Basic', 'S0', 'S1', 'S2', 'GP_Gen5_2', 'GP_Gen5_4'])
param sqlDatabaseSku string = 'GP_Gen5_2'

@description('Storage account SKU.')
@allowed(['Standard_LRS', 'Standard_GRS', 'Standard_ZRS', 'Standard_RAGRS'])
param storageAccountSku string = 'Standard_ZRS'

@description('Log Analytics workspace retention in days.')
@minValue(30)
@maxValue(730)
param logRetentionDays int = 90

@description('Enable DDoS protection on the hub VNet.')
param enableDdosProtection bool = false

@description('Tags to apply to all resources. Should include: Environment, Project, ManagedBy, LastDeployed.')
param tags object = {}

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var baseName = '${projectName}-${environmentName}'
var uniqueSuffix = uniqueString(resourceGroup().id, projectName, environmentName)

var hubVnetName = 'vnet-hub-${baseName}'
var spokeVnetName = 'vnet-spoke-${baseName}'
var firewallName = 'afw-${baseName}'
var firewallPipName = 'pip-afw-${baseName}'
var bastionName = 'bas-${baseName}'
var bastionPipName = 'pip-bas-${baseName}'
var nsgHubMgmtName = 'nsg-hub-mgmt-${baseName}'
var nsgSpokeAppName = 'nsg-spoke-app-${baseName}'
var nsgSpokeDataName = 'nsg-spoke-data-${baseName}'
var nsgSpokeIntegrationName = 'nsg-spoke-int-${baseName}'
var routeTableName = 'rt-spoke-${baseName}'

var logAnalyticsName = 'log-${baseName}-${uniqueSuffix}'
var appInsightsName = 'appi-${baseName}'

var keyVaultName = 'kv-${take(baseName, 14)}-${take(uniqueSuffix, 6)}'
var storageAccountName = 'st${replace(baseName, '-', '')}${take(uniqueSuffix, 6)}'

var appServicePlanName = 'asp-${baseName}'
var webAppName = 'app-${baseName}-${take(uniqueSuffix, 6)}'
var sqlServerName = 'sql-${baseName}-${take(uniqueSuffix, 6)}'
var sqlDatabaseName = 'sqldb-${baseName}'

var defaultTags = union({
  Environment: environmentName
  Project: projectName
  ManagedBy: 'Bicep'
  LastDeployed: utcNow('yyyy-MM-dd')
}, tags)

// ---------------------------------------------------------------------------
// Log Analytics Workspace (central logging and monitoring)
// ---------------------------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: defaultTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    workspaceCapping: {
      dailyQuotaGb: 5
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Application Insights (application performance monitoring)
// ---------------------------------------------------------------------------

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: defaultTags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    RetentionInDays: logRetentionDays
  }
}

// ---------------------------------------------------------------------------
// Network Security Groups (segmentation and access control)
// ---------------------------------------------------------------------------

resource nsgHubMgmt 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: nsgHubMgmtName
  location: location
  tags: defaultTags
  properties: {
    securityRules: [
      {
        name: 'AllowRDP'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: bastionSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '3389'
        }
      }
      {
        name: 'AllowSSH'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: bastionSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
      {
        name: 'DenyAllInbound'
        properties: {
          priority: 4096
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource nsgSpokeApp 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: nsgSpokeAppName
  location: location
  tags: defaultTags
  properties: {
    securityRules: [
      {
        name: 'AllowHTTPS'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'AllowHTTP'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '80'
        }
      }
      {
        name: 'AllowAppServiceManagement'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: 'AppServiceManagement'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '454-455'
        }
      }
      {
        name: 'DenyAllInbound'
        properties: {
          priority: 4096
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource nsgSpokeData 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: nsgSpokeDataName
  location: location
  tags: defaultTags
  properties: {
    securityRules: [
      {
        name: 'AllowSQLFromApp'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: spokeAppSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '1433'
        }
      }
      {
        name: 'AllowStorageFromApp'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: spokeAppSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '445'
        }
      }
      {
        name: 'DenyAllInbound'
        properties: {
          priority: 4096
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource nsgSpokeIntegration 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: nsgSpokeIntegrationName
  location: location
  tags: defaultTags
  properties: {
    securityRules: [
      {
        name: 'AllowVNetInbound'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
        }
      }
      {
        name: 'DenyAllInbound'
        properties: {
          priority: 4096
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// NSG Diagnostic Settings (send NSG logs to Log Analytics)
// ---------------------------------------------------------------------------

resource nsgHubMgmtDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${nsgHubMgmtName}'
  scope: nsgHubMgmt
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
  }
}

resource nsgSpokeAppDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${nsgSpokeAppName}'
  scope: nsgSpokeApp
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
  }
}

resource nsgSpokeDataDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${nsgSpokeDataName}'
  scope: nsgSpokeData
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
  }
}

resource nsgSpokeIntDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${nsgSpokeIntegrationName}'
  scope: nsgSpokeIntegration
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
  }
}

// ---------------------------------------------------------------------------
// Route Table (force-tunnel spoke traffic through Azure Firewall)
// ---------------------------------------------------------------------------

resource routeTable 'Microsoft.Network/routeTables@2024-01-01' = {
  name: routeTableName
  location: location
  tags: defaultTags
  properties: {
    disableBgpRoutePropagation: true
    routes: [
      {
        name: 'DefaultToFirewall'
        properties: {
          addressPrefix: '0.0.0.0/0'
          nextHopType: 'VirtualAppliance'
          nextHopIpAddress: firewall.properties.ipConfigurations[0].properties.privateIPAddress
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Hub Virtual Network (shared services hub VNet)
// ---------------------------------------------------------------------------

resource hubVnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: hubVnetName
  location: location
  tags: defaultTags
  properties: {
    addressSpace: {
      addressPrefixes: [hubVnetAddressSpace]
    }
    enableDdosProtection: enableDdosProtection
    subnets: [
      {
        name: 'AzureFirewallSubnet'
        properties: {
          addressPrefix: firewallSubnetPrefix
        }
      }
      {
        name: 'AzureBastionSubnet'
        properties: {
          addressPrefix: bastionSubnetPrefix
        }
      }
      {
        name: 'snet-hub-mgmt'
        properties: {
          addressPrefix: hubMgmtSubnetPrefix
          networkSecurityGroup: {
            id: nsgHubMgmt.id
          }
        }
      }
    ]
  }
}

resource hubVnetDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${hubVnetName}'
  scope: hubVnet
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

// ---------------------------------------------------------------------------
// Spoke Virtual Network (application workloads VNet)
// ---------------------------------------------------------------------------

resource spokeVnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: spokeVnetName
  location: location
  tags: defaultTags
  properties: {
    addressSpace: {
      addressPrefixes: [spokeVnetAddressSpace]
    }
    subnets: [
      {
        name: 'snet-app'
        properties: {
          addressPrefix: spokeAppSubnetPrefix
          networkSecurityGroup: {
            id: nsgSpokeApp.id
          }
          routeTable: {
            id: routeTable.id
          }
          delegations: [
            {
              name: 'appServiceDelegation'
              properties: {
                serviceName: 'Microsoft.Web/serverFarms'
              }
            }
          ]
        }
      }
      {
        name: 'snet-data'
        properties: {
          addressPrefix: spokeDataSubnetPrefix
          networkSecurityGroup: {
            id: nsgSpokeData.id
          }
          routeTable: {
            id: routeTable.id
          }
          serviceEndpoints: [
            { service: 'Microsoft.Sql' }
            { service: 'Microsoft.Storage' }
            { service: 'Microsoft.KeyVault' }
          ]
        }
      }
      {
        name: 'snet-integration'
        properties: {
          addressPrefix: spokeIntegrationSubnetPrefix
          networkSecurityGroup: {
            id: nsgSpokeIntegration.id
          }
          routeTable: {
            id: routeTable.id
          }
          delegations: [
            {
              name: 'appServiceDelegation'
              properties: {
                serviceName: 'Microsoft.Web/serverFarms'
              }
            }
          ]
        }
      }
    ]
  }
}

resource spokeVnetDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${spokeVnetName}'
  scope: spokeVnet
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

// ---------------------------------------------------------------------------
// VNet Peering: Hub <-> Spoke (bidirectional connectivity)
// ---------------------------------------------------------------------------

resource hubToSpokePeering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-01-01' = {
  name: 'peer-hub-to-spoke'
  parent: hubVnet
  properties: {
    remoteVirtualNetwork: {
      id: spokeVnet.id
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

resource spokeToHubPeering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-01-01' = {
  name: 'peer-spoke-to-hub'
  parent: spokeVnet
  properties: {
    remoteVirtualNetwork: {
      id: hubVnet.id
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

// ---------------------------------------------------------------------------
// Azure Firewall (central egress control)
// ---------------------------------------------------------------------------

resource firewallPip 'Microsoft.Network/publicIPAddresses@2024-01-01' = {
  name: firewallPipName
  location: location
  tags: defaultTags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource firewall 'Microsoft.Network/azureFirewalls@2024-01-01' = {
  name: firewallName
  location: location
  tags: defaultTags
  properties: {
    sku: {
      name: 'AZFW_VNet'
      tier: 'Standard'
    }
    threatIntelMode: 'Deny'
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          publicIPAddress: {
            id: firewallPip.id
          }
          subnet: {
            id: hubVnet.properties.subnets[0].id
          }
        }
      }
    ]
    networkRuleCollections: [
      {
        name: 'net-rules-allow-spoke'
        properties: {
          priority: 100
          action: { type: 'Allow' }
          rules: [
            {
              name: 'AllowSpokeToInternet'
              protocols: ['TCP', 'UDP']
              sourceAddresses: [spokeVnetAddressSpace]
              destinationAddresses: ['*']
              destinationPorts: ['80', '443']
            }
            {
              name: 'AllowDNS'
              protocols: ['UDP']
              sourceAddresses: [spokeVnetAddressSpace]
              destinationAddresses: ['*']
              destinationPorts: ['53']
            }
            {
              name: 'AllowNTP'
              protocols: ['UDP']
              sourceAddresses: [spokeVnetAddressSpace]
              destinationAddresses: ['*']
              destinationPorts: ['123']
            }
          ]
        }
      }
    ]
    applicationRuleCollections: [
      {
        name: 'app-rules-allow-azure'
        properties: {
          priority: 100
          action: { type: 'Allow' }
          rules: [
            {
              name: 'AllowAzureServices'
              protocols: [
                { protocolType: 'Https', port: 443 }
              ]
              sourceAddresses: [spokeVnetAddressSpace]
              targetFqdns: [
                '*.azure.com'
                '*.microsoft.com'
                '*.windows.net'
                '*.microsoftonline.com'
                '*.azure-api.net'
              ]
            }
            {
              name: 'AllowWindowsUpdate'
              protocols: [
                { protocolType: 'Https', port: 443 }
                { protocolType: 'Http', port: 80 }
              ]
              sourceAddresses: [hubVnetAddressSpace, spokeVnetAddressSpace]
              targetFqdns: [
                '*.windowsupdate.com'
                '*.update.microsoft.com'
                '*.download.windowsupdate.com'
              ]
            }
          ]
        }
      }
    ]
  }
}

resource firewallDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${firewallName}'
  scope: firewall
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

// ---------------------------------------------------------------------------
// Azure Bastion (secure RDP/SSH access)
// ---------------------------------------------------------------------------

resource bastionPip 'Microsoft.Network/publicIPAddresses@2024-01-01' = {
  name: bastionPipName
  location: location
  tags: defaultTags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource bastion 'Microsoft.Network/bastionHosts@2024-01-01' = {
  name: bastionName
  location: location
  tags: defaultTags
  sku: {
    name: 'Standard'
  }
  properties: {
    enableTunneling: true
    enableFileCopy: true
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          publicIPAddress: {
            id: bastionPip.id
          }
          subnet: {
            id: hubVnet.properties.subnets[1].id
          }
        }
      }
    ]
  }
}

resource bastionDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${bastionName}'
  scope: bastion
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
  }
}

// ---------------------------------------------------------------------------
// Key Vault (central secrets management)
// ---------------------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: defaultTags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
      virtualNetworkRules: [
        {
          id: spokeVnet.properties.subnets[1].id
        }
      ]
    }
  }
}

resource keyVaultDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${keyVaultName}'
  scope: keyVault
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

resource sqlPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'sql-admin-password'
  parent: keyVault
  properties: {
    value: sqlAdminPassword
    contentType: 'text/plain'
    attributes: {
      enabled: true
    }
  }
}

resource sqlConnectionStringSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'sql-connection-string'
  parent: keyVault
  properties: {
    value: 'Server=tcp:${sqlServer.properties.fullyQualifiedDomainName},1433;Initial Catalog=${sqlDatabaseName};Persist Security Info=False;User ID=${sqlAdminLogin};Password=${sqlAdminPassword};MultipleActiveResultSets=False;Encrypt=True;TrustServerCertificate=False;Connection Timeout=30;'
    contentType: 'text/plain'
    attributes: {
      enabled: true
    }
  }
}

resource appInsightsKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'appinsights-connection-string'
  parent: keyVault
  properties: {
    value: appInsights.properties.ConnectionString
    contentType: 'text/plain'
    attributes: {
      enabled: true
    }
  }
}

// ---------------------------------------------------------------------------
// Storage Account (application data and backups)
// ---------------------------------------------------------------------------

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: defaultTags
  sku: {
    name: storageAccountSku
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
      virtualNetworkRules: [
        {
          id: spokeVnet.properties.subnets[1].id
          action: 'Allow'
        }
      ]
    }
    encryption: {
      keySource: 'Microsoft.Storage'
      services: {
        blob: { enabled: true, keyType: 'Account' }
        file: { enabled: true, keyType: 'Account' }
        table: { enabled: true, keyType: 'Account' }
        queue: { enabled: true, keyType: 'Account' }
      }
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  name: 'default'
  parent: storageAccount
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 30
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 30
    }
    isVersioningEnabled: true
  }
}

resource appDataContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  name: 'app-data'
  parent: blobService
  properties: {
    publicAccess: 'None'
  }
}

resource backupsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  name: 'backups'
  parent: blobService
  properties: {
    publicAccess: 'None'
  }
}

resource storageDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${storageAccountName}'
  scope: blobService
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { category: 'StorageRead', enabled: true }
      { category: 'StorageWrite', enabled: true }
      { category: 'StorageDelete', enabled: true }
    ]
    metrics: [
      { category: 'Transaction', enabled: true }
    ]
  }
}

// ---------------------------------------------------------------------------
// App Service Plan (compute for App Service)
// ---------------------------------------------------------------------------

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  tags: defaultTags
  sku: {
    name: appServicePlanSku
  }
  kind: 'linux'
  properties: {
    reserved: true
    zoneRedundant: environmentName == 'prod'
  }
}

// ---------------------------------------------------------------------------
// Web App (primary application deployment)
// ---------------------------------------------------------------------------

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  tags: union(defaultTags, { 'hidden-related:${appServicePlan.id}': 'empty' })
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    virtualNetworkSubnetId: spokeVnet.properties.subnets[2].id
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'DOTNETCORE|8.0'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      healthCheckPath: '/health'
      appSettings: [
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'ApplicationInsightsAgent_EXTENSION_VERSION'
          value: '~3'
        }
        {
          name: 'WEBSITE_RUN_FROM_PACKAGE'
          value: '1'
        }
        {
          name: 'KeyVaultUri'
          value: keyVault.properties.vaultUri
        }
      ]
    }
  }
}

resource webAppStagingSlot 'Microsoft.Web/sites/slots@2023-12-01' = {
  name: 'staging'
  parent: webApp
  location: location
  tags: defaultTags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    virtualNetworkSubnetId: spokeVnet.properties.subnets[2].id
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'DOTNETCORE|8.0'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      healthCheckPath: '/health'
      autoSwapSlotName: 'production'
    }
  }
}

resource webAppDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${webAppName}'
  scope: webApp
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { category: 'AppServiceHTTPLogs', enabled: true }
      { category: 'AppServiceConsoleLogs', enabled: true }
      { category: 'AppServiceAppLogs', enabled: true }
      { category: 'AppServicePlatformLogs', enabled: true }
      { category: 'AppServiceAuditLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

// ---------------------------------------------------------------------------
// SQL Server and Database (data tier)
// ---------------------------------------------------------------------------

resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: sqlServerName
  location: location
  tags: defaultTags
  properties: {
    administratorLogin: sqlAdminLogin
    administratorLoginPassword: sqlAdminPassword
    version: '12.0'
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Disabled'
  }
}

resource sqlVNetRule 'Microsoft.Sql/servers/virtualNetworkRules@2023-08-01-preview' = {
  name: 'allow-spoke-data-subnet'
  parent: sqlServer
  properties: {
    virtualNetworkSubnetId: spokeVnet.properties.subnets[1].id
    ignoreMissingVnetServiceEndpoint: false
  }
}

resource sqlDatabase 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  name: sqlDatabaseName
  parent: sqlServer
  location: location
  tags: defaultTags
  sku: {
    name: sqlDatabaseSku
  }
  properties: {
    collation: 'SQL_Latin1_General_CP1_CI_AS'
    maxSizeBytes: 34359738368 // 32 GB
    zoneRedundant: environmentName == 'prod'
    readScale: environmentName == 'prod' ? 'Enabled' : 'Disabled'
    requestedBackupStorageRedundancy: environmentName == 'prod' ? 'Geo' : 'Local'
    isLedgerOn: false
  }
}

resource sqlDbDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${sqlDatabaseName}'
  scope: sqlDatabase
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      { category: 'SQLInsights', enabled: true }
      { category: 'AutomaticTuning', enabled: true }
      { category: 'QueryStoreRuntimeStatistics', enabled: true }
      { category: 'QueryStoreWaitStatistics', enabled: true }
      { category: 'Errors', enabled: true }
      { category: 'DatabaseWaitStatistics', enabled: true }
      { category: 'Timeouts', enabled: true }
      { category: 'Blocks', enabled: true }
      { category: 'Deadlocks', enabled: true }
    ]
    metrics: [
      { category: 'Basic', enabled: true }
      { category: 'InstanceAndAppAdvanced', enabled: true }
      { category: 'WorkloadManagement', enabled: true }
    ]
  }
}

resource sqlAuditSettings 'Microsoft.Sql/servers/auditingSettings@2023-08-01-preview' = {
  name: 'default'
  parent: sqlServer
  properties: {
    state: 'Enabled'
    isAzureMonitorTargetEnabled: true
    retentionDays: logRetentionDays
  }
}

resource sqlThreatDetection 'Microsoft.Sql/servers/securityAlertPolicies@2023-08-01-preview' = {
  name: 'default'
  parent: sqlServer
  properties: {
    state: 'Enabled'
    emailAccountAdmins: true
  }
}

resource sqlTdePolicyProtector 'Microsoft.Sql/servers/databases/transparentDataEncryption@2023-08-01-preview' = {
  name: 'current'
  parent: sqlDatabase
  properties: {
    state: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// RBAC Role Assignments (least privilege for managed identities)
// ---------------------------------------------------------------------------

// Web App → Key Vault Secrets User
resource webAppKeyVaultRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, webApp.id, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: keyVault
  properties: {
    principalId: webApp.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalType: 'ServicePrincipal'
  }
}

// Web App → Storage Blob Data Contributor
resource webAppStorageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, webApp.id, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: storageAccount
  properties: {
    principalId: webApp.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalType: 'ServicePrincipal'
  }
}

// Staging Slot → Key Vault Secrets User
resource stagingSlotKeyVaultRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, webAppStagingSlot.id, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: keyVault
  properties: {
    principalId: webAppStagingSlot.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalType: 'ServicePrincipal'
  }
}

// Staging Slot → Storage Blob Data Contributor
resource stagingSlotStorageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, webAppStagingSlot.id, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: storageAccount
  properties: {
    principalId: webAppStagingSlot.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Alert Rules (key health and reliability signals)
// ---------------------------------------------------------------------------

resource highCpuAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-high-cpu-${webAppName}'
  location: 'global'
  tags: defaultTags
  properties: {
    description: 'Alert when CPU percentage exceeds 80%'
    severity: 2
    enabled: true
    scopes: [appServicePlan.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'HighCPU'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'CpuPercentage'
          metricNamespace: 'Microsoft.Web/serverfarms'
          operator: 'GreaterThan'
          threshold: 80
          timeAggregation: 'Average'
        }
      ]
    }
  }
}

resource highMemoryAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-high-memory-${webAppName}'
  location: 'global'
  tags: defaultTags
  properties: {
    description: 'Alert when memory percentage exceeds 80%'
    severity: 2
    enabled: true
    scopes: [appServicePlan.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'HighMemory'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'MemoryPercentage'
          metricNamespace: 'Microsoft.Web/serverfarms'
          operator: 'GreaterThan'
          threshold: 80
          timeAggregation: 'Average'
        }
      ]
    }
  }
}

resource httpErrorsAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-http-5xx-${webAppName}'
  location: 'global'
  tags: defaultTags
  properties: {
    description: 'Alert when HTTP 5xx errors exceed threshold'
    severity: 1
    enabled: true
    scopes: [webApp.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'Http5xxErrors'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'Http5xx'
          metricNamespace: 'Microsoft.Web/sites'
          operator: 'GreaterThan'
          threshold: 10
          timeAggregation: 'Total'
        }
      ]
    }
  }
}

resource sqlDtuAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-sql-dtu-${sqlDatabaseName}'
  location: 'global'
  tags: defaultTags
  properties: {
    description: 'Alert when SQL DTU consumption exceeds 80%'
    severity: 2
    enabled: true
    scopes: [sqlDatabase.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'HighDTU'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'dtu_consumption_percent'
          metricNamespace: 'Microsoft.Sql/servers/databases'
          operator: 'GreaterThan'
          threshold: 80
          timeAggregation: 'Average'
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output hubVnetId string = hubVnet.id
output spokeVnetId string = spokeVnet.id
output firewallPrivateIp string = firewall.properties.ipConfigurations[0].properties.privateIPAddress
output bastionId string = bastion.id
output logAnalyticsWorkspaceId string = logAnalytics.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output keyVaultUri string = keyVault.properties.vaultUri
output storageAccountName string = storageAccount.name
output webAppDefaultHostName string = webApp.properties.defaultHostName
output webAppPrincipalId string = webApp.identity.principalId
output sqlServerFqdn string = sqlServer.properties.fullyQualifiedDomainName
output sqlDatabaseName string = sqlDatabase.name
