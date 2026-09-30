#!/usr/bin/env zsh
# teardown-server.zsh — remove one Zulip server.
#
#   scripts/teardown-server.zsh --config <dept> --server <name> [--purge]
#
# Default (preserve): deletes the app and its -mgmt and -hc jobs. Keeps the database, the /data
# share (uploads), the -dbinit job and the Key Vault secrets, so deploy-server.zsh brings
# it back exactly as it was.
# --purge: also drops the database and role, deletes the share, the storage link, the
# -dbinit job, the server's two managed identities and its Key Vault secrets.
# Irreversible (Key Vault keeps deleted secrets recoverable for 90 days).
# Always type-to-confirm (CHAT_CONFIRM for CI). The Entra app registration is left for
# an operator to delete; the command is printed.
source "${0:A:h}/common.zsh"
PURGE=false
parse_common_args "$@"
for a in "${CHAT_ARGS_REST[@]}"; do
  case "$a" in --purge) PURGE=true ;; *) die "unknown argument: $a" ;; esac
done
load_platform
load_server
az_login

if $PURGE; then
  log_warn "PURGE: $SERVER's database, uploads and secrets will be destroyed"
  confirm_typed "PURGE $SERVER"
else
  log_info "preserve mode: database, uploads and secrets are kept"
  confirm_typed "CONFIRM-DELETE $SERVER"
fi

if app_exists; then
  if ! $PURGE; then
    # Keep the hostname bindings so deploy-server.zsh restores the server exactly.
    domains="$(az containerapp show -g "$RG" -n "$APP_NAME" --query 'properties.configuration.ingress.customDomains' -o json 2>/dev/null || true)"
    if [[ -n "$domains" && "$domains" != null && "$domains" != "[]" ]]; then
      print -rn -- "$domains" | jq -c . | kv_secret_set_from_stdin "$SERVER-custom-domains"
      log_ok "saved $(print -r -- "$domains" | jq length) hostname binding(s) for the next deploy"
    fi
  fi
  az_retry containerapp delete -g "$RG" -n "$APP_NAME" --yes --output none
  log_ok "deleted app $APP_NAME"
else
  log_info "app $APP_NAME already gone"
fi
for job in "$MGMT_JOB" "$(jqs .jobs.hc)"; do
  if az containerapp job show -g "$RG" -n "$job" >/dev/null 2>&1; then
    az_retry containerapp job delete -g "$RG" -n "$job" --yes --output none
    log_ok "deleted job $job"
  fi
done

if $PURGE; then
  if az containerapp job show -g "$RG" -n "$DBINIT_JOB" >/dev/null 2>&1; then
    # Reuse the dbinit job with DB_ACTION=drop; trust the job's own report, not its status.
    RUN_JOB_ENV=(DB_ACTION=drop)
    RUN_JOB_EXPECT=".dropped == true and .database == \"$(jqs .database.name)\"" run_job "$DBINIT_JOB" dbinit >/dev/null \
      || die "dropping the database failed; nothing else was purged"
    RUN_JOB_ENV=()
    log_ok "dropped database $(jqs .database.name)"
    az_retry containerapp job delete -g "$RG" -n "$DBINIT_JOB" --yes --output none
  fi
  if az containerapp env storage show -g "$RG" -n "$ENV_NAME" --storage-name "$(jqs .env_storage)" >/dev/null 2>&1; then
    az_retry containerapp env storage remove -g "$RG" -n "$ENV_NAME" --storage-name "$(jqs .env_storage)" --yes --output none
    log_ok "removed storage link $(jqs .env_storage)"
  fi
  if az storage share-rm show -g "$RG" --storage-account "$ST_NAME" --name "$(jqs .share)" >/dev/null 2>&1; then
    az_retry storage share-rm delete -g "$RG" --storage-account "$ST_NAME" --name "$(jqs .share)" --yes --output none
    log_ok "deleted share $(jqs .share)"
  else
    log_info "share $(jqs .share) already gone"
  fi
  for kind in app db; do
    ident="$(jqs ".identities.$kind.name")"
    if az identity show -g "$RG" -n "$ident" >/dev/null 2>&1; then
      az identity delete -g "$RG" -n "$ident" --output none
      log_ok "deleted identity $ident"
    fi
  done
  for s in "${(@f)$(jq -r '.key_vault_secrets | to_entries[] | select(.value | startswith("'"$SERVER"'-")) | .value' "$SERVER_JSON")}" "$SERVER-oidc-client-id" "$SERVER-custom-domains"; do
    if kv_secret_exists "$s"; then
      az keyvault secret delete --vault-name "$KV_NAME" --name "$s" --output none
      log_ok "deleted secret $s (soft-deleted; recoverable for 90 days)"
    fi
  done
fi

APP_REG="$(az ad app list --display-name "$PREFIX-$SERVER-zulip" --query '[0].appId' -o tsv 2>/dev/null || true)"
[[ -n "$APP_REG" ]] && print -u2 -r -- "Entra app registration kept. To remove it: az ad app delete --id $APP_REG"
print -u2 -r -- "If $SERVER had DNS records, ask OIT to remove: $(jq -r '.hosts | join(", ")' "$SERVER_JSON")"
summary "- teardown $SERVER ($($PURGE && print purge || print preserve)) complete"
