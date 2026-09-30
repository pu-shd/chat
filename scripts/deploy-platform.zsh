#!/usr/bin/env zsh
# deploy-platform.zsh — create or update a department's shared platform
# (infra/platform.bicep): resource group, Key Vault, VNet, Container Apps environment,
# PostgreSQL Flexible Server, NFS storage, Log Analytics and the Key Vault reader identity.
#
#   scripts/deploy-platform.zsh --config <dept-dir> [--yes] [--dry-run]
#
# Run by an operator with Owner on the subscription or resource group (it creates a role
# assignment). Idempotent: rerunning updates in place and never rotates the PostgreSQL
# admin password it vaulted the first time.
source "${0:A:h}/common.zsh"
parse_common_args "$@"
(( ${#CHAT_ARGS_REST} == 0 )) || die "unknown argument: ${CHAT_ARGS_REST[1]}"
load_platform
require_cmd openssl
az_login

log_step "Platform for $(jqp .department) in $RG ($LOCATION)"

log_step "1/5 Resource group"
if az group show -n "$RG" >/dev/null 2>&1; then
  log_ok "$RG exists"
else
  if $DRY_RUN; then log_info "dry run: would create $RG, $KV_NAME and the platform"; exit 0; fi
  confirm "Create resource group $RG in $LOCATION?" || die "aborted"
  az group create -n "$RG" -l "$LOCATION" --tags "chat-department=$(jqp .department)" --output none
  log_ok "$RG created"
fi

log_step "2/5 Key Vault (RBAC)"
if az keyvault show -n "$KV_NAME" -g "$RG" >/dev/null 2>&1; then
  log_ok "$KV_NAME exists"
else
  if az keyvault list-deleted --query "[?name=='$KV_NAME'].name" -o tsv 2>/dev/null | grep -qx "$KV_NAME"; then
    die "$KV_NAME is soft-deleted; recover it (az keyvault recover -n $KV_NAME) or purge it before reusing the name"
  fi
  if $DRY_RUN; then log_info "dry run: would create $KV_NAME and the platform"; exit 0; fi
  # Purge protection: a deleted secret (or vault) stays recoverable for 90 days, even
  # from an identity with Secrets Officer. The name stays reserved for that long.
  az keyvault create -n "$KV_NAME" -g "$RG" -l "$LOCATION" --enable-rbac-authorization true \
    --retention-days 90 --enable-purge-protection true --output none
  log_ok "$KV_NAME created"
fi
KV_ID="$(az keyvault show -n "$KV_NAME" -g "$RG" --query id -o tsv)"
if ! $DRY_RUN && [[ "$(az account show --query user.type -o tsv)" == user ]]; then
  ME="$(az ad signed-in-user show --query id -o tsv)"
  if [[ -z "$(az role assignment list --assignee "$ME" --scope "$KV_ID" --role 'Key Vault Secrets Officer' --query '[0].id' -o tsv)" ]]; then
    az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
      --role 'Key Vault Secrets Officer' --scope "$KV_ID" --output none
    log_ok "granted you Key Vault Secrets Officer on $KV_NAME"
  fi
fi
# RBAC takes a minute to propagate; wait until we can actually list secrets.
for i in {1..18}; do
  az keyvault secret list --vault-name "$KV_NAME" --maxresults 1 --output none 2>/dev/null && break
  if (( i == 18 )); then die "cannot read secrets in $KV_NAME after 3 minutes (role assignment not effective?)"; fi
  sleep 10
done

log_step "3/5 Platform secrets"
if $DRY_RUN; then
  kv_secret_exists pg-admin-password || { log_info "dry run: would generate pg-admin-password and deploy"; exit 0; }
else
  kv_secret_ensure_random pg-admin-password
fi
if kv_secret_exists email-password; then
  log_ok "email-password present"
else
  gh_warning "Key Vault has no email-password (Resend API key); servers will not deploy until it exists — bootstrap.zsh prompts for it"
fi

log_step "4/5 Deploy infra/platform.bicep"
PARAMS="$(mktemp)"; chmod 600 "$PARAMS"
trap 'rm -f "$PARAMS"' EXIT
ACS_NAME="$(jq -r '.acs.communication_service // empty' "$PLATFORM_JSON")"
LINKED='[]'
if [[ -n "$ACS_NAME" ]]; then
  # Keep domains acs-email.zsh already linked; a redeploy must not unlink them.
  LINKED="$(az communication show -g "$RG" -n "$ACS_NAME" --query 'linkedDomains' -o json 2>/dev/null || true)"
  [[ -n "$LINKED" && "$LINKED" != null ]] || LINKED='[]'
fi
kv_secret_get pg-admin-password | jq -Rs --slurpfile p "$PLATFORM_JSON" --argjson linked "$LINKED" '{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  contentVersion: "1.0.0.0",
  parameters: {
    location: {value: $p[0].region},
    environmentName: {value: $p[0].names.environment},
    storageAccountName: {value: $p[0].names.storage},
    registryName: {value: $p[0].names.registry},
    postgresName: {value: $p[0].names.postgres},
    logAnalyticsName: {value: $p[0].names.log_analytics},
    identityName: {value: $p[0].names.identity},
    vnetName: {value: $p[0].names.vnet},
    vnetCidr: {value: $p[0].network.vnet},
    acaSubnetCidr: {value: $p[0].network.aca_subnet},
    pgSubnetCidr: {value: $p[0].network.pg_subnet},
    postgresAdminUser: {value: $p[0].postgres.admin_user},
    postgresAdminPassword: {value: rtrimstr("\n")},
    postgresSku: {value: $p[0].postgres.sku},
    postgresVersion: {value: $p[0].postgres.version},
    tags: {value: {"chat-department": $p[0].department}},
    acs: {value: ($p[0].acs // {})},
    acsLinkedDomains: {value: $linked},
    mailFromName: {value: $p[0].email.from_name}
  }}' > "$PARAMS"
if $DRY_RUN; then
  az deployment group what-if -g "$RG" -n "chat-platform" --template-file "$CHAT_ROOT/infra/platform.bicep" --parameters "@$PARAMS"
  exit 0
fi
OUT="$(az_retry deployment group create -g "$RG" -n "chat-platform" \
  --template-file "$CHAT_ROOT/infra/platform.bicep" --parameters "@$PARAMS" \
  --query properties.outputs -o json)"
log_ok "platform deployed"

log_step "5/5 Outputs"
DEFAULT_DOMAIN="$(print -r -- "$OUT" | jq -er .defaultDomain.value)"
VERIFY_ID="$(print -r -- "$OUT" | jq -er .customDomainVerificationId.value)"
print -r -- "  environment default domain : $DEFAULT_DOMAIN"
print -r -- "  custom domain verification : $VERIFY_ID"
print -r -- "  static inbound IP          : $(print -r -- "$OUT" | jq -r .staticIp.value)"
print -r -- "  container registry         : $(print -r -- "$OUT" | jq -r .registryLoginServer.value)"
summary "### Platform \`$RG\`"
summary "- default domain: \`$DEFAULT_DOMAIN\` — servers are reachable at \`<app>.$DEFAULT_DOMAIN\` before any DNS exists"
summary "- asuid TXT value for custom domains: \`$VERIFY_ID\`"
if [[ -n "$ACS_NAME" ]]; then
  log_info "email: Azure Communication Services $ACS_NAME — next: acs-email.zsh --config $CONFIG_DIR (SMTP credentials; DNS verification for a custom domain)"
fi
