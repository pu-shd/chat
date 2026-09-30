// server.bicep — one Zulip server: a Container App (Zulip + redis/memcached/rabbitmq
// sidecars, exactly one replica), its NFS /data share, and two manual jobs:
//   <app>-dbinit  create/update its database and role on the shared PostgreSQL
//   <app>-mgmt    run chat-manage (realms, push registration) against the live app
//
// Parameters come from `tools/render.py resolve`. State that lives outside git —
// bound hostnames/certificates and IP-gate rules — is read back from the live app by
// scripts/deploy-server.zsh and passed in, so a redeploy never drops it.
//
// deployApp=false creates everything except the app, so the database exists before
// Zulip's first boot tries to migrate it.

param location string
param environmentName string
@description('Per-server identities (created and granted per secret by grant-access.zsh)')
param appIdentityName string
param dbIdentityName string
param keyVaultName string
param storageAccountName string
param postgresHost string
param postgresAdminUser string
param acaSubnet string
param tenantId string

param serverName string
param appName string
param mgmtJobName string
param dbinitJobName string
param hcJobName string
@description('Healthchecks.io: a scheduled job pings <ping base>/<key>/<slug> after GET /health')
param healthchecksEnabled bool = false
param healthchecksPingBase string = 'https://hc-ping.com'
param healthchecksSlug string = ''
param healthchecksCron string = '*/5 * * * *'
param databaseName string
param shareName string
param envStorageName string
param dataQuotaGiB int = 100
param zulipCpu string
param zulipMemory string
@description('Zulip image in the department registry, pinned by digest')
param image string
@description('Department Azure Container Registry login server; pulled with the managed identities')
param registryServer string
@description('docker-zulip environment: [{name, value}]')
param zulipEnv array
@description('ACA secret name -> Key Vault secret name')
param keyVaultSecretNames object
param customDomains array = []
param ipSecurityRestrictions array = []
param easyAuth bool = false
param easyAuthExcludedPaths array = []
param oidcClientId string
param deployApp bool = true

@description('Sidecar images in the department registry (render.py resolve maps image/sidecars.json onto it)')
param sidecarImages object
param tags object = {}

var sidecarCpu = json('0.25')
var sidecarMemory = '0.5Gi'
var vaultUri = 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/'

resource env 'Microsoft.App/managedEnvironments@2025-01-01' existing = {
  name: environmentName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: appIdentityName
}
resource dbIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: dbIdentityName
}
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}
resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: shareName
  properties: {
    enabledProtocols: 'NFS'
    // docker-zulip's entrypoint runs as root and chowns /data/uploads.
    rootSquash: 'NoRootSquash'
    shareQuota: dataQuotaGiB
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2025-01-01' = {
  parent: env
  name: envStorageName
  properties: {
    nfsAzureFile: {
      server: '${storageAccountName}.file.${environment().suffixes.storage}'
      shareName: '/${storageAccountName}/${shareName}'
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [ share ]
}

// ---------------------------------------------------------------- secrets

var appSecretNames = [ 'secret-key', 'postgres-password', 'redis-password', 'rabbitmq-password', 'memcached-password', 'oidc-secret', 'email-password' ]
var appSecrets = [for s in appSecretNames: {
  name: s
  keyVaultUrl: '${vaultUri}${keyVaultSecretNames[s]}'
  identity: identity.id
}]
var dbinitSecrets = [for s in [ 'postgres-password', 'pg-admin-password' ]: {
  name: s
  keyVaultUrl: '${vaultUri}${keyVaultSecretNames[s]}'
  identity: dbIdentity.id
}]
// docker-zulip reads /run/secrets/zulip__<key> and overrides zulip-secrets.conf.
var zulipSecretFiles = [
  { secretRef: 'secret-key', path: 'zulip__secret_key' }
  { secretRef: 'postgres-password', path: 'zulip__postgres_password' }
  { secretRef: 'redis-password', path: 'zulip__redis_password' }
  { secretRef: 'rabbitmq-password', path: 'zulip__rabbitmq_password' }
  { secretRef: 'memcached-password', path: 'zulip__memcached_password' }
  { secretRef: 'oidc-secret', path: 'zulip__social_auth_oidc_secret' }
  { secretRef: 'email-password', path: 'zulip__email_password' }
]
var zulipVolumes = [
  { name: 'data', storageType: 'NfsAzureFile', storageName: envStorage.name }
  { name: 'zulip-secrets', storageType: 'Secret', secrets: zulipSecretFiles }
]
var zulipMounts = [
  { volumeName: 'data', mountPath: '/data' }
  { volumeName: 'zulip-secrets', mountPath: '/run/secrets' }
]

// The -mgmt job runs in its own replica, so it reaches the app's sidecars through the
// environment's internal network instead of localhost.
var sidecarHostKeys = [ 'SETTING_REDIS_HOST', 'SETTING_RABBITMQ_HOST', 'SETTING_MEMCACHED_LOCATION' ]
var mgmtEnv = concat(filter(zulipEnv, e => !contains(sidecarHostKeys, e.name)), [
  { name: 'SETTING_REDIS_HOST', value: appName }
  { name: 'SETTING_RABBITMQ_HOST', value: appName }
  { name: 'SETTING_MEMCACHED_LOCATION', value: '${appName}:11211' }
  { name: 'AUTO_BACKUP_ENABLED', value: 'False' }
])

// ---------------------------------------------------------------- app

var memcachedScript = '''
mkdir -p /home/memcache
echo 'mech_list: plain' > "$SASL_CONF_PATH"
echo "zulip@$HOSTNAME:$MEMCACHED_PASSWORD" > "$MEMCACHED_SASL_PWDB"
echo "zulip@localhost:$MEMCACHED_PASSWORD" >> "$MEMCACHED_SASL_PWDB"
exec memcached -S -m 256
'''
var rabbitmqScript = '''
mkdir -p /etc/rabbitmq/conf.d
printf 'default_user = zulip\ndefault_pass = %s\n' "$RABBITMQ_PASSWORD" > /etc/rabbitmq/conf.d/10-chat.conf
exec docker-entrypoint.sh rabbitmq-server
'''

resource app 'Microsoft.App/containerApps@2025-01-01' = if (deployApp) {
  name: appName
  location: location
  tags: union(tags, { 'chat-server': serverName })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [ { server: registryServer, identity: identity.id } ]
      secrets: appSecrets
      ingress: {
        external: true
        targetPort: 80
        transport: 'http'
        allowInsecure: false
        traffic: [ { latestRevision: true, weight: 100 } ]
        customDomains: customDomains
        ipSecurityRestrictions: ipSecurityRestrictions
        additionalPortMappings: [
          { external: false, targetPort: 6379, exposedPort: 6379 }
          { external: false, targetPort: 5672, exposedPort: 5672 }
          { external: false, targetPort: 11211, exposedPort: 11211 }
          // Plain HTTP to Zulip's nginx for the -hc probe: the main ingress redirects
          // http:// to https:// (allowInsecure: false), which a probe must not follow.
          { external: false, targetPort: 80, exposedPort: 8080 }
        ]
      }
    }
    template: {
      // Tornado is single-process; one replica, always on.
      scale: { minReplicas: 1, maxReplicas: 1 }
      volumes: zulipVolumes
      containers: [
        {
          name: 'zulip'
          image: image
          resources: { cpu: json(zulipCpu), memory: zulipMemory }
          env: zulipEnv
          volumeMounts: zulipMounts
          probes: [
            {
              // First boot runs puppet and every migration: allow ~11 minutes.
              type: 'Startup'
              httpGet: { path: '/health', port: 80 }
              initialDelaySeconds: 60
              periodSeconds: 60
              failureThreshold: 10
              timeoutSeconds: 5
            }
            {
              type: 'Liveness'
              httpGet: { path: '/health', port: 80 }
              periodSeconds: 30
              failureThreshold: 5
              timeoutSeconds: 5
            }
          ]
        }
        {
          name: 'redis'
          image: sidecarImages.redis
          resources: { cpu: sidecarCpu, memory: sidecarMemory }
          command: [ 'sh', '-c' ]
          // Password in a 0600 config file, not on redis-server's command line.
          args: [ 'umask 077 && printf \'requirepass %s\nbind 0.0.0.0\nprotected-mode no\nsave ""\nappendonly no\n\' "$REDIS_PASSWORD" > /tmp/redis.conf && exec redis-server /tmp/redis.conf' ]
          env: [ { name: 'REDIS_PASSWORD', secretRef: 'redis-password' } ]
        }
        {
          name: 'memcached'
          image: sidecarImages.memcached
          resources: { cpu: sidecarCpu, memory: sidecarMemory }
          command: [ 'sh', '-euc', memcachedScript ]
          env: [
            { name: 'MEMCACHED_PASSWORD', secretRef: 'memcached-password' }
            { name: 'SASL_CONF_PATH', value: '/home/memcache/memcached.conf' }
            { name: 'MEMCACHED_SASL_PWDB', value: '/home/memcache/memcached-sasl-db' }
          ]
        }
        {
          name: 'rabbitmq'
          image: sidecarImages.rabbitmq
          resources: { cpu: sidecarCpu, memory: sidecarMemory }
          command: [ 'sh', '-euc', rabbitmqScript ]
          env: [ { name: 'RABBITMQ_PASSWORD', secretRef: 'rabbitmq-password' } ]
        }
      ]
    }
  }
}

// Optional Entra Easy Auth perimeter. Zulip's own OIDC login stays the real gate for
// everything in easyAuthExcludedPaths (API, uploads, SCIM, mobile sign-in).
resource auth 'Microsoft.App/containerApps/authConfigs@2025-01-01' = if (deployApp && easyAuth) {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: easyAuthExcludedPaths
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: oidcClientId
          clientSecretSettingName: 'oidc-secret'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: { allowedAudiences: [ oidcClientId, 'api://${oidcClientId}' ] }
      }
    }
    login: { preserveUrlFragmentsForLogins: true }
  }
}

// ---------------------------------------------------------------- jobs

resource mgmt 'Microsoft.App/jobs@2025-01-01' = {
  name: mgmtJobName
  location: location
  tags: union(tags, { 'chat-server': serverName })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [ { server: registryServer, identity: identity.id } ]
      secrets: appSecrets
    }
    template: {
      volumes: zulipVolumes
      containers: [
        {
          name: 'mgmt'
          image: image
          resources: { cpu: json('1.0'), memory: '2Gi' }
          command: [ '/usr/local/bin/chat-entrypoint' ]
          args: [ 'chat:manage', 'list-realms' ]
          env: mgmtEnv
          volumeMounts: zulipMounts
        }
      ]
    }
  }
}

resource dbinit 'Microsoft.App/jobs@2025-01-01' = {
  name: dbinitJobName
  location: location
  tags: union(tags, { 'chat-server': serverName })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${dbIdentity.id}': {} }
  }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [ { server: registryServer, identity: dbIdentity.id } ]
      secrets: dbinitSecrets
    }
    template: {
      volumes: [
        {
          name: 'db-secrets'
          storageType: 'Secret'
          secrets: [
            { secretRef: 'pg-admin-password', path: 'pg-admin-password' }
            { secretRef: 'postgres-password', path: 'postgres-password' }
          ]
        }
      ]
      containers: [
        {
          name: 'dbinit'
          image: sidecarImages.postgres
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          command: [ 'sh', '-c', loadTextContent('../image/bin/chat-dbinit') ]
          env: [
            { name: 'PGHOST', value: postgresHost }
            { name: 'PGUSER', value: postgresAdminUser }
            { name: 'PGADMIN_PASSWORD_FILE', value: '/run/db-secrets/pg-admin-password' }
            { name: 'DB_NAME', value: databaseName }
            { name: 'DB_USER', value: databaseName }
            { name: 'DB_PASSWORD_FILE', value: '/run/db-secrets/postgres-password' }
            { name: 'DB_ACTION', value: 'ensure' }
          ]
          volumeMounts: [ { volumeName: 'db-secrets', mountPath: '/run/db-secrets' } ]
        }
      ]
    }
  }
}

// Healthchecks.io probe from inside the environment (works with the IP gate and before DNS).
// Created with the app (not in the infra phase), so a first deploy never pages before
// Zulip exists.
resource hc 'Microsoft.App/jobs@2025-01-01' = if (healthchecksEnabled && deployApp) {
  name: hcJobName
  location: location
  tags: union(tags, { 'chat-server': serverName })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 120
      replicaRetryLimit: 0
      scheduleTriggerConfig: { cronExpression: healthchecksCron, parallelism: 1, replicaCompletionCount: 1 }
      registries: [ { server: registryServer, identity: identity.id } ]
      secrets: [
        { name: 'hc-ping-key', keyVaultUrl: '${vaultUri}healthchecks-ping-key', identity: identity.id }
      ]
    }
    template: {
      volumes: [ { name: 'hc', storageType: 'Secret', secrets: [ { secretRef: 'hc-ping-key', path: 'ping-key' } ] } ]
      containers: [
        {
          name: 'hc'
          image: sidecarImages.curl
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          command: [ 'sh', '-c', loadTextContent('../image/bin/chat-hc-ping') ]
          env: [
            { name: 'CHAT_HC_TARGET', value: 'http://${appName}:8080/health' }
            { name: 'HC_PING_BASE', value: healthchecksPingBase }
            { name: 'HC_SLUG', value: healthchecksSlug }
            { name: 'HC_PING_KEY_FILE', value: '/run/hc/ping-key' }
          ]
          volumeMounts: [ { volumeName: 'hc', mountPath: '/run/hc' } ]
        }
      ]
    }
  }
}

output appFqdn string = deployApp ? app!.properties.configuration.ingress.fqdn : ''
output latestRevision string = deployApp ? app!.properties.latestRevisionName : ''
output acaSubnet string = acaSubnet
