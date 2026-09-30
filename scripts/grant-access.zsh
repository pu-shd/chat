#!/usr/bin/env zsh
# grant-access.zsh — least-privilege Key Vault access for one server (operator; creates
# role assignments, which CI cannot).
#
#   scripts/grant-access.zsh --config <dept> --server <name>
#
# Creates the server's two managed identities if missing —
#   <app>-id     the Zulip app, its -mgmt and -hc jobs
#   <app>-db-id  only the -dbinit job
# — generates the server's random secrets if missing (never replacing one), then grants
# each identity Key Vault Secrets User on exactly the secrets listed for it in
# generated/servers/<name>.json, and AcrPull on the department registry. Nothing gets
# vault-wide access, so a compromised
# Zulip server cannot read another server's secrets or the PostgreSQL admin password.
# Idempotent; rerun after enabling Healthchecks (adds healthchecks-ping-key).
source "${0:A:h}/common.zsh"
parse_common_args "$@"
(( ${#CHAT_ARGS_REST} == 0 )) || die "unknown argument: ${CHAT_ARGS_REST[1]}"
load_platform
load_server
require_cmd openssl
az_login
ROLE="Key Vault Secrets User"
KV_ID="$(az keyvault show -n "$KV_NAME" -g "$RG" --query id -o tsv)"
[[ -n "$KV_ID" ]] || die "Key Vault $KV_NAME not found; run deploy-platform.zsh first"

log_step "Secrets for $SERVER"
for s in secret-key postgres-password redis-password rabbitmq-password memcached-password; do
  kv_secret_ensure_random "$SERVER-$s"
done
missing=()
for kind in app db; do
  for s in "${(@f)$(jqs ".identities.$kind.secrets[]")}"; do
    kv_secret_exists "$s" || missing+=("$s")
  done
done
missing=("${(@u)missing}")
(( ${#missing} == 0 )) || die "create these first: ${(j:, :)missing} (email-password: bootstrap.zsh --only secrets; $SERVER-oidc-secret: entra-app.zsh; healthchecks-ping-key: bootstrap.zsh --only secrets)"

for kind in app db; do
  name="$(jqs ".identities.$kind.name")"
  log_step "Identity $name"
  if ! az identity show -g "$RG" -n "$name" >/dev/null 2>&1; then
    az identity create -g "$RG" -n "$name" -l "$LOCATION" --tags "chat-server=$SERVER" --output none
    log_ok "created"
  fi
  pid="$(az identity show -g "$RG" -n "$name" --query principalId -o tsv)"
  [[ -n "$pid" ]] || die "$name has no principal id"
  for s in "${(@f)$(jqs ".identities.$kind.secrets[]")}"; do
    scope="$KV_ID/secrets/$s"
    if [[ -n "$(az role assignment list --assignee "$pid" --scope "$scope" --role "$ROLE" --query '[0].id' -o tsv 2>/dev/null)" ]]; then
      log_ok "$s: already granted"
    else
      # A fresh identity can take a few seconds to replicate in Entra.
      for i in {1..6}; do
        az role assignment create --assignee-object-id "$pid" --assignee-principal-type ServicePrincipal \
          --role "$ROLE" --scope "$scope" --output none 2>/dev/null && break
        if (( i == 6 )); then die "could not grant $name read on $s"; fi
        sleep 10
      done
      log_ok "$s: granted"
    fi
  done
done
# Both identities pull their images from the department registry.
ACR_ID="$(az acr show -n "$(jqp .names.registry)" -g "$RG" --query id -o tsv)"
[[ -n "$ACR_ID" ]] || die "registry $(jqp .names.registry) not found; run deploy-platform.zsh first"
for kind in app db; do
  name="$(jqs ".identities.$kind.name")"
  pid="$(az identity show -g "$RG" -n "$name" --query principalId -o tsv)"
  if [[ -n "$(az role assignment list --assignee "$pid" --scope "$ACR_ID" --role AcrPull --query '[0].id' -o tsv 2>/dev/null)" ]]; then
    log_ok "$name: AcrPull already granted"
  else
    for i in {1..6}; do
      az role assignment create --assignee-object-id "$pid" --assignee-principal-type ServicePrincipal \
        --role AcrPull --scope "$ACR_ID" --output none 2>/dev/null && break
      if (( i == 6 )); then die "could not grant $name AcrPull"; fi
      sleep 10
    done
    log_ok "$name: AcrPull granted"
  fi
done
summary "- access for $SERVER: $(jqs '.identities.app.secrets | length') secrets for the app, $(jqs '.identities.db.secrets | length') for dbinit"
