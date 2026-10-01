#!/usr/bin/env zsh
# teardown-platform.zsh — delete a department's whole resource group.
#
#   scripts/teardown-platform.zsh --config <dept> [--purge-vault]
#
# Refuses while any Container App or job is left in the environment (tear servers down
# first). Type-to-confirm. Key Vault is soft-deleted (90 days) unless --purge-vault.
source "${0:A:h}/common.zsh"
PURGE_VAULT=false
parse_common_args "$@"
for a in "${CHAT_ARGS_REST[@]}"; do
  case "$a" in --purge-vault) PURGE_VAULT=true ;; *) die "unknown argument: $a" ;; esac
done
load_platform
az_login
az_exists group show -n "$RG" --query id -o tsv || { log_info "$RG does not exist"; exit 0; }

# Two checked reads: a failed list must never read as "nothing left".
apps="$(az_retry containerapp list -g "$RG" --query '[].name' -o tsv)" || die "could not list the Container Apps in $RG"
jobs="$(az_retry containerapp job list -g "$RG" --query '[].name' -o tsv)" || die "could not list the Container Apps jobs in $RG"
left="${apps//[[:space:]]/}${jobs//[[:space:]]/}"
if [[ -n "$left" ]]; then
  die "servers remain in $RG; run teardown-server.zsh --purge for each first: ${apps//$'\n'/ } ${jobs//$'\n'/ }"
fi
confirm_typed "DELETE-PLATFORM $RG"
az group delete -n "$RG" --yes
log_ok "deleted $RG"
if $PURGE_VAULT; then
  log_warn "$KV_NAME has purge protection: it cannot be purged; it is recoverable and its name reserved for 90 days"
fi
log_info "$KV_NAME is soft-deleted (purge protection on): recover with az keyvault recover -n $KV_NAME within 90 days"
if APP="$(entra_app_by_name "$(jqp .names.github_app)")"; then
  [[ -n "$APP" ]] && print -u2 -r -- "GitHub Actions app registration kept: az ad app delete --id $APP"
else
  log_warn "GitHub Actions app registration $(jqp .names.github_app): see above; not printing a delete command"
fi
exit 0
