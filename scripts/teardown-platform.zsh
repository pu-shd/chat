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
az group show -n "$RG" >/dev/null 2>&1 || { log_info "$RG does not exist"; exit 0; }

left="$(az containerapp list -g "$RG" --query '[].name' -o tsv) $(az containerapp job list -g "$RG" --query '[].name' -o tsv)"
left="${left// /}"
if [[ -n "${left//$'\n'/}" ]]; then
  die "servers remain in $RG; run teardown-server.zsh --purge for each first: $(az containerapp list -g "$RG" --query '[].name' -o tsv | tr '\n' ' ')"
fi
confirm_typed "DELETE-PLATFORM $RG"
az group delete -n "$RG" --yes
log_ok "deleted $RG"
if $PURGE_VAULT; then
  log_warn "$KV_NAME has purge protection: it cannot be purged; it is recoverable and its name reserved for 90 days"
fi
log_info "$KV_NAME is soft-deleted (purge protection on): recover with az keyvault recover -n $KV_NAME within 90 days"
APP="$(az ad app list --display-name "$(jqp .names.github_app)" --query '[0].appId' -o tsv 2>/dev/null || true)"
[[ -n "$APP" ]] && print -u2 -r -- "GitHub Actions app registration kept: az ad app delete --id $APP"
exit 0
