#!/usr/bin/env zsh
# teardown-server.zsh — remove one Zulip server.
#
#   scripts/teardown-server.zsh --config <dept> --server <name> [--purge]
#
# Default (preserve): deletes the app and its -mgmt and -hc jobs. Keeps the database, the /data
# share (uploads), the -dbinit job and the Key Vault secrets, so deploy-server.zsh brings
# it back exactly as it was.
# --purge: also drops the database and role, deletes the share, the storage link, the
# -dbinit job, the server's managed identities (app, db, hc) and its Key Vault secrets.
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
    # Keep the hostname bindings so deploy-server.zsh restores the server exactly. They
    # must be read before the app goes: a failed read stops the teardown. An app with
    # none saves [] too, so an older save cannot bring back bindings removed since.
    domains="$(az_retry containerapp show -g "$RG" -n "$APP_NAME" --query 'properties.configuration.ingress.customDomains' -o json)" \
      || die "could not read $APP_NAME's hostname bindings; nothing was deleted"
    domains="$(print -r -- "${domains:-null}" | jq -c '. // []')"
    print -rn -- "$domains" | kv_secret_set_from_stdin "$SERVER-custom-domains"
    log_ok "saved $(print -r -- "$domains" | jq length) hostname binding(s) for the next deploy"
  fi
  az_retry containerapp delete -g "$RG" -n "$APP_NAME" --yes --output none
  log_ok "deleted app $APP_NAME"
else
  log_info "app $APP_NAME already gone"
fi
for job in "$MGMT_JOB" "$(jqs .jobs.hc)"; do
  if az_exists containerapp job show -g "$RG" -n "$job" --query id -o tsv; then
    az_retry containerapp job delete -g "$RG" -n "$job" --yes --output none
    log_ok "deleted job $job"
  fi
done

if $PURGE; then
  if az_exists containerapp job show -g "$RG" -n "$DBINIT_JOB" --query id -o tsv; then
    # Reuse the dbinit job with DB_ACTION=drop; trust the job's own report, not its status.
    RUN_JOB_ENV=(DB_ACTION=drop)
    RUN_JOB_EXPECT=".dropped == true and .database == \"$(jqs .database.name)\"" run_job "$DBINIT_JOB" dbinit >/dev/null \
      || die "dropping the database failed; nothing else was purged"
    RUN_JOB_ENV=()
    log_ok "dropped database $(jqs .database.name)"
    az_retry containerapp job delete -g "$RG" -n "$DBINIT_JOB" --yes --output none
  fi
  if az_exists containerapp env storage show -g "$RG" -n "$ENV_NAME" --storage-name "$(jqs .env_storage)" --query id -o tsv; then
    az_retry containerapp env storage remove -g "$RG" -n "$ENV_NAME" --storage-name "$(jqs .env_storage)" --yes --output none
    log_ok "removed storage link $(jqs .env_storage)"
  fi
  if az_exists storage share-rm show -g "$RG" --storage-account "$ST_NAME" --name "$(jqs .share)" --query id -o tsv; then
    az_retry storage share-rm delete -g "$RG" --storage-account "$ST_NAME" --name "$(jqs .share)" --yes --output none
    log_ok "deleted share $(jqs .share)"
  else
    log_info "share $(jqs .share) already gone"
  fi
  # Every per-server identity (app, db, and hc when healthchecks are on); <app>-hc-id
  # also when healthchecks were turned off after it was created.
  typeset -aU idents
  idents=("${(@f)$(jq -r '.identities[].name' "$SERVER_JSON")}" "$APP_NAME-hc-id")
  for ident in "${idents[@]}"; do
    if az_exists identity show -g "$RG" -n "$ident" --query id -o tsv; then
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

# Only a hint, so an ambiguous or foreign app is a warning here, never a delete command.
if APP_REG="$(entra_app_by_name "$PREFIX-$SERVER-zulip")"; then
  [[ -n "$APP_REG" ]] && print -u2 -r -- "Entra app registration kept. To remove it: az ad app delete --id $APP_REG"
else
  log_warn "Entra app registration $PREFIX-$SERVER-zulip: see above; not printing a delete command"
fi
print -u2 -r -- "If $SERVER had DNS records, ask your DNS administrators to remove: $(jq -r '.hosts | join(", ")' "$SERVER_JSON")"
summary "- teardown $SERVER ($($PURGE && print purge || print preserve)) complete"
