// platform.bicep — one per department: network, Container Apps environment, shared
// PostgreSQL, NFS storage, logs and the identity every server uses to read Key Vault.
//
// Deployed by scripts/deploy-platform.zsh. The Key Vault itself is created by that
// script first, so the PostgreSQL admin password can be vaulted before the server
// that uses it exists.

@description('Azure region')
param location string
param environmentName string
param storageAccountName string
param postgresName string
param logAnalyticsName string
param identityName string
param vnetName string
param vnetCidr string
param acaSubnetCidr string
param pgSubnetCidr string
param postgresAdminUser string
@secure()
param postgresAdminPassword string
param postgresSku string = 'Standard_B1ms'
param postgresVersion string = '17'
param tags object = {}
@description('Azure Communication Services Email (platform.json "acs"); {} when mail goes elsewhere')
param acs object = {}
@description('Domains already linked to the Communication Services resource (kept on redeploy; a custom domain is linked by acs-email.zsh once its DNS verifies)')
param acsLinkedDomains array = []
param mailFromName string = 'Chat'

var pgTier = startsWith(postgresSku, 'Standard_B') ? 'Burstable' : (contains(postgresSku, 'E') ? 'MemoryOptimized' : 'GeneralPurpose')

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

// The environment's identity imports TLS certificates from Key Vault. It gets no
// vault-wide role: bind-domain.zsh grants it read on each certificate it imports.
// Apps and jobs use their own per-server identities (grant-access.zsh).

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [ vnetCidr ] }
    subnets: [
      {
        name: 'snet-aca'
        properties: {
          addressPrefix: acaSubnetCidr
          delegations: [ { name: 'aca', properties: { serviceName: 'Microsoft.App/environments' } } ]
          // NFS Azure Files is reachable only from networks the storage account allows.
          serviceEndpoints: [ { service: 'Microsoft.Storage' } ]
        }
      }
      {
        name: 'snet-pg'
        properties: {
          addressPrefix: pgSubnetCidr
          delegations: [ { name: 'pg', properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' } } ]
        }
      }
    ]
  }
}

resource pgDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: '${postgresName}.private.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource pgDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: pgDns
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresName
  location: location
  tags: tags
  sku: { name: postgresSku, tier: pgTier }
  properties: {
    version: postgresVersion
    administratorLogin: postgresAdminUser
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: 32, autoGrow: 'Enabled' }
    backup: { backupRetentionDays: 14, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    authConfig: { passwordAuth: 'Enabled', activeDirectoryAuth: 'Disabled' }
    network: {
      delegatedSubnetResourceId: vnet.properties.subnets[1].id
      privateDnsZoneArmResourceId: pgDns.id
      publicNetworkAccess: 'Disabled'
    }
  }
  dependsOn: [ pgDnsLink ]
}

// Premium FileStorage for NFS shares (one per Zulip server, created by server.bicep).
// NFS has no transport encryption, so https-only must be off; access is limited to the
// Container Apps subnet instead.
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'FileStorage'
  sku: { name: 'Premium_LRS' }
  properties: {
    supportsHttpsTrafficOnly: false
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      virtualNetworkRules: [ { id: vnet.properties.subnets[0].id, action: 'Allow' } ]
    }
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: environmentName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    workloadProfiles: [ { name: 'Consumption', workloadProfileType: 'Consumption' } ]
    vnetConfiguration: {
      infrastructureSubnetId: vnet.properties.subnets[0].id
      internal: false
    }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// ---------------------------------------------------------------- email (optional)

var useAcs = !empty(acs)
var acsManaged = acs.?managed ?? false

resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = if (useAcs) {
  name: acs.?email_service ?? 'unused-email'
  location: 'global'
  tags: tags
  properties: { dataLocation: acs.?data_location ?? 'United States' }
}

resource emailDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = if (useAcs) {
  parent: emailService
  name: acs.?domain ?? 'unused.example'
  location: 'global'
  tags: tags
  properties: {
    domainManagement: acsManaged ? 'AzureManaged' : 'CustomerManaged'
    userEngagementTracking: 'Disabled'
  }
}

// Custom domains send only from configured sender usernames (Azure-managed: DoNotReply).
resource senders 'Microsoft.Communication/emailServices/domains/senderUsernames@2023-04-01' = [for s in (useAcs ? (acs.?senders ?? []) : []): {
  parent: emailDomain
  name: s
  properties: { username: s, displayName: mailFromName }
}]

resource communication 'Microsoft.Communication/communicationServices@2023-04-01' = if (useAcs) {
  name: acs.?communication_service ?? 'unused-acs'
  location: 'global'
  tags: tags
  properties: {
    dataLocation: acs.?data_location ?? 'United States'
    // An Azure-managed domain is verified at creation and can be linked at once.
    linkedDomains: acsManaged ? [ emailDomain.id ] : acsLinkedDomains
  }
}

output environmentId string = environment.id
output defaultDomain string = environment.properties.defaultDomain
output staticIp string = environment.properties.staticIp
output customDomainVerificationId string = environment.properties.customDomainConfiguration.customDomainVerificationId
output identityId string = identity.id
output identityClientId string = identity.properties.clientId
output logAnalyticsCustomerId string = logs.properties.customerId
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
output acsMailFrom string = useAcs && acsManaged ? 'DoNotReply@${emailDomain!.properties.mailFromSenderDomain}' : ''
output acsDomainId string = useAcs ? emailDomain.id : ''
