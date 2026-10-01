#!/usr/bin/env zsh
# deploy-server.zsh — create or update one Zulip server (infra/server.bicep).
#
#   scripts/deploy-server.zsh --config <dept-dir> --server <name> [--lock <template.lock>]
#                             [--image <registry>/chat@sha256:...] [--allow-unpinned-image]
#                             [--skip-realms] [--dry-run] [--yes]
#
# The image defaults to the department registry's chat:<ref>-<sha7> for template.lock
# (build it first with build-image.zsh), deployed by digest.
#
# Order, and why:
#   1. per-server secrets exist in Key Vault (generated once, never replaced); operator
#      secrets (Resend key, Entra client secret, PG admin) must already be there;
#   2. read back what lives outside git — bound hostnames/certificates and the IP rules —
#      so the redeploy keeps them;
#   3. deploy everything but the app, then run the -dbinit job, so the database and role
#      exist before Zulip's first boot migrates;
#   4. if the image changes, deactivate the running revision first: Container Apps would
#      otherwise run old and new Zulip side by side while the new one migrates;
#   5. deploy the app and wait for its revision to be healthy;
#   6. make sure every active realm exists (the -mgmt job).
#
# Works before DNS exists: with dns: pending the server answers on
# https://<app>.<environment default domain>/ and that is what it is configured for.
source "${0:A:h}/common.zsh"
IMAGE="${CHAT_IMAGE:-}" LOCK="" ALLOW_UNPINNED=false SKIP_REALMS=false
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --image) IMAGE="${2:?}"; shift 2 ;;
    --lock) LOCK="${2:?}"; shift 2 ;;
    --allow-unpinned-image) ALLOW_UNPINNED=true; shift ;;
    --skip-realms) SKIP_REALMS=true; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
load_platform
load_server
az_login

# The image is the one build-image.zsh built into this department's registry from the
# pinned template commit (tag chat:<ref>-<sha7>, locked); deploy it by digest.
ACR="$(jqp .names.registry)"
REGISTRY="$(az acr show -n "$ACR" -g "$RG" --query loginServer -o tsv 2>/dev/null || true)"
[[ -n "$REGISTRY" ]] || die "registry $ACR not found; run deploy-platform.zsh first"
if [[ -z "$IMAGE" ]]; then
  LOCK="${LOCK:-$CONFIG_DIR/../template.lock}"
  [[ -f "$LOCK" ]] || die "no --image and no template.lock at $LOCK"
  TAG="chat:$(jq -er .ref "$LOCK")-$(jq -er .sha "$LOCK" | cut -c1-7)"
  DIGEST="$(az acr repository show -n "$ACR" --image "$TAG" --query digest -o tsv 2>/dev/null || true)"
  [[ "$DIGEST" == sha256:* ]] || die "$REGISTRY/$TAG is not built yet; run build-image.zsh --config $CONFIG_DIR"
  IMAGE="$REGISTRY/chat@$DIGEST"
fi
[[ "$IMAGE" == "$REGISTRY/chat@sha256:"* ]] || $ALLOW_UNPINNED || die "image must come from $REGISTRY by digest: $IMAGE"
log_ok "image $IMAGE"

log_step "Server $SERVER → $APP_NAME ($(jqs .kind), dns: $(jqs .dns))"
az containerapp env show -g "$RG" -n "$ENV_NAME" --query id -o tsv >/dev/null 2>&1 \
  || die "Container Apps environment $ENV_NAME not found; run deploy-platform.zsh first"

log_step "1/6 Secrets"
typeset -A KVS
KVS=("${(@f)$(jq -r '.key_vault_secrets | to_entries[] | .key, .value' "$SERVER_JSON")}")
missing=()
for aca in "${(@k)KVS}"; do
  kv_secret_exists "${KVS[$aca]}" || missing+=("${KVS[$aca]}")
done
if hc_enabled; then
  kv_secret_exists healthchecks-ping-key || missing+=(healthchecks-ping-key)
fi
missing=("${(@o)missing}")
(( ${#missing} == 0 )) || die "Key Vault $KV_NAME lacks ${(j:, :)missing} — run bootstrap.zsh (email-password), entra-app.zsh --server $SERVER (oidc-secret), deploy-platform.zsh (pg-admin-password), bootstrap.zsh --only secrets (healthchecks-ping-key), grant-access.zsh --server $SERVER (the server's own)"

# Each identity must be able to read exactly its secrets (grant-access.zsh, operator).
KV_ID="$(az keyvault show -n "$KV_NAME" -g "$RG" --query id -o tsv)"
no_access=()
# app, db, and hc (the healthchecks pinger) when the server JSON has it.
KINDS=("${(@f)$(jqs '.identities | keys[]')}")
for kind in "${KINDS[@]}"; do
  ident="$(jqs ".identities.$kind.name")"
  pid="$(az identity show -g "$RG" -n "$ident" --query principalId -o tsv 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then no_access+=("$ident (missing)"); continue; fi
  for s in "${(@f)$(jqs ".identities.$kind.secrets[]")}"; do
    [[ -n "$s" ]] || continue  # never a scope of the whole vault
    [[ -n "$(az role assignment list --assignee "$pid" --scope "$KV_ID/secrets/$s" --role 'Key Vault Secrets User' --query '[0].id' -o tsv 2>/dev/null)" ]] \
      || no_access+=("$ident → $s")
  done
done
ACR_ID="$(az acr show -n "$ACR" -g "$RG" --query id -o tsv)"
for kind in "${KINDS[@]}"; do
  ident="$(jqs ".identities.$kind.name")"
  pid="$(az identity show -g "$RG" -n "$ident" --query principalId -o tsv 2>/dev/null || true)"
  [[ -z "$pid" ]] && continue
  [[ -n "$(az role assignment list --assignee "$pid" --scope "$ACR_ID" --role AcrPull --query '[0].id' -o tsv 2>/dev/null)" ]] \
    || no_access+=("$ident → AcrPull on $ACR")
done
(( ${#no_access} == 0 )) || die "missing access: ${(j:, :)no_access} — an operator runs grant-access.zsh --config $CONFIG_DIR --server $SERVER"
CLIENT_ID="$(jq -r '.entra.client_id // empty' "$SERVER_JSON")"
if [[ -z "$CLIENT_ID" ]]; then
  CLIENT_ID="$(kv_secret_get "$SERVER-oidc-client-id" 2>/dev/null || true)"
fi
[[ -n "$CLIENT_ID" ]] || die "no Entra client id for $SERVER: run entra-app.zsh --server $SERVER"
log_ok "secrets and client id present"

log_step "2/6 Live state to preserve"
DOMAIN="$(env_default_domain)"
[[ -n "$DOMAIN" ]] || die "could not read $ENV_NAME's default domain"
LIVE_IMAGE="" CUSTOM_DOMAINS='[]' RESTORED_DOMAINS=false
# app_exists ends the script on an error that is not "not found": guessing "first deploy"
# would drop the bound hostnames and skip the maintenance window.
if app_exists; then
  LIVE="$(az containerapp show -g "$RG" -n "$APP_NAME" -o json)"
  LIVE_IMAGE="$(print -r -- "$LIVE" | jq -r '.properties.template.containers[] | select(.name=="zulip") | .image')"
  CUSTOM_DOMAINS="$(print -r -- "$LIVE" | jq -c '.properties.configuration.ingress.customDomains // []')"
  log_ok "exists: image ${LIVE_IMAGE:-?}, $(print -r -- "$CUSTOM_DOMAINS" | jq length) bound hostname(s) kept"
else
  log_info "first deploy of $APP_NAME"
  # A preserve-mode teardown saved the bound hostnames; bring them back with the app.
  saved=""
  if kv_secret_exists "$SERVER-custom-domains"; then
    saved="$(kv_secret_get "$SERVER-custom-domains")" || die "could not read $SERVER-custom-domains from $KV_NAME"
  fi
  if [[ -n "$saved" ]] && print -r -- "$saved" | jq -e 'type == "array" and length > 0' >/dev/null 2>&1; then
    CUSTOM_DOMAINS="$(print -r -- "$saved" | jq -c .)"
    RESTORED_DOMAINS=true
    log_ok "restoring $(print -r -- "$CUSTOM_DOMAINS" | jq length) hostname binding(s) saved at teardown"
  fi
fi
IP_RULES='[]'
if [[ "$(jqs .ip_gate)" == true ]]; then
  IP_RULES="$("${0:A:h}/ip-gate.zsh" --config "$CONFIG_DIR" --server "$SERVER" --emit | jq -c .)"
fi

ACS_MAIL_FROM=""
if [[ "$(jq -r '.acs.managed // false' "$PLATFORM_JSON")" == true ]]; then
  # Azure-managed ACS domain: the sender is DoNotReply@<generated>.azurecomm.net.
  ACS_MAIL_FROM="DoNotReply@$(az communication email domain show -g "$RG" --email-service-name "$(jqp .acs.email_service)" \
    -n "$(jqp .acs.domain)" --query mailFromSenderDomain -o tsv)"
  [[ "$ACS_MAIL_FROM" == DoNotReply@?*.?* ]] || die "could not read the ACS Azure-managed sender domain; run deploy-platform.zsh"
fi

params() {  # params <deployApp true|false> -> path of a resolved ARM parameters file
  local f
  f="$(mktemp)"
  local extra=()
  $ALLOW_UNPINNED && extra+=(--allow-unpinned-image)
  [[ -n "$ACS_MAIL_FROM" ]] && extra+=(--acs-mail-from "$ACS_MAIL_FROM")
  "$CHAT_PY" "$CHAT_RENDER" resolve --server "$SERVER_JSON" --default-domain "$DOMAIN" \
    --image "$IMAGE" --registry "$REGISTRY" --oidc-client-id "$CLIENT_ID" --custom-domains "$CUSTOM_DOMAINS" \
    --ip-rules "$IP_RULES" --deploy-app "$1" "${extra[@]}" > "$f"
  print -r -- "$f"
}
deploy() {  # deploy <name-suffix> <params-file>
  az_retry deployment group create -g "$RG" -n "chat-$SERVER-$1" \
    --template-file "$CHAT_ROOT/infra/server.bicep" --parameters "@$2" \
    --query properties.outputs -o json
}

P_INFRA="" P_APP=""
trap 'rm -f "$P_INFRA" "$P_APP"' EXIT
P_INFRA="$(params false)"
P_APP="$(params true)"
if $DRY_RUN; then
  log_step "dry run: what-if"
  az deployment group what-if -g "$RG" -n "chat-$SERVER-app" \
    --template-file "$CHAT_ROOT/infra/server.bicep" --parameters "@$P_APP"
  exit 0
fi

log_step "3/6 Share, storage link, jobs; database"
deploy infra "$P_INFRA" >/dev/null
log_ok "infrastructure deployed"
DB_RESULT="$(RUN_JOB_EXPECT=".database == \"$(jqs .database.name)\" and .collation == \"C.UTF-8\"" run_job "$DBINIT_JOB" dbinit)"
log_ok "database: $DB_RESULT"

log_step "4/6 Maintenance window"
if [[ -n "$LIVE_IMAGE" && "$LIVE_IMAGE" != "$IMAGE" ]]; then
  log_warn "image changes $LIVE_IMAGE → $IMAGE; stopping the running revision so only one Zulip migrates"
  RESTORE_POINT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  summary "- **$SERVER**: upgrade from \`$LIVE_IMAGE\`; PostgreSQL point-in-time restore target before the upgrade: \`$RESTORE_POINT\` (server \`$PG_NAME\`)"
  # Listed first and checked: a failed list inside the for words would be silent and run
  # old and new Zulip side by side while the new one migrates.
  REVS_RAW="$(az_retry containerapp revision list -g "$RG" -n "$APP_NAME" --query '[?properties.active].name' -o tsv)" \
    || die "could not list $APP_NAME's active revisions; not starting $IMAGE beside a running Zulip"
  typeset -a REVS
  REVS=("${(@f)REVS_RAW}"); REVS=("${(@)REVS:#}")
  (( ${#REVS} )) || log_info "no active revision to stop"
  for rev in "${REVS[@]}"; do
    az_retry containerapp revision deactivate -g "$RG" -n "$APP_NAME" --revision "$rev" --output none
    log_ok "deactivated $rev"
  done
else
  log_ok "no image change; rolling update"
fi

log_step "5/6 Deploy the app"
OUT="$(deploy app "$P_APP")"
FQDN="$(print -r -- "$OUT" | jq -er .appFqdn.value)"
REVISION="$(print -r -- "$OUT" | jq -er .latestRevision.value)"
log_info "waiting for revision $REVISION (first boot can take ~10 minutes; up to ${HEALTH_TIMEOUT:-1500}s)"
waited=0 unhealthy=0
while true; do
  state="$(az containerapp revision show -g "$RG" -n "$APP_NAME" --revision "$REVISION" \
    --query '[properties.healthState, properties.runningState]' -o tsv 2>/dev/null | tr '\n\t' '  ' || true)"
  case "$state" in
    *Healthy*Running*|*Healthy*RunningAtMaxScale*) break ;;
    *Failed*|*Degraded*)
      az containerapp logs show -g "$RG" -n "$APP_NAME" --revision "$REVISION" --container zulip --tail 60 2>/dev/null || true
      die "revision $REVISION is $state" ;;
    *Unhealthy*)
      # Probes fail while first boot runs puppet and migrations, and the startup probe
      # (infra/server.bicep) allows about 21 minutes for that: only Unhealthy for about
      # as long (66 polls of 20 s = 22 min) is a failure. Failed/Degraded stop at once.
      unhealthy=$(( unhealthy + 1 ))
      if (( unhealthy >= ${UNHEALTHY_LIMIT:-66} )); then
        az containerapp logs show -g "$RG" -n "$APP_NAME" --revision "$REVISION" --container zulip --tail 60 2>/dev/null || true
        die "revision $REVISION is $state"
      fi ;;
    *) unhealthy=0 ;;
  esac
  if (( waited >= ${HEALTH_TIMEOUT:-1500} )); then
    die "revision $REVISION not healthy after ${waited}s (state: ${state:-unknown})"
  fi
  sleep "${HEALTH_POLL:-20}"; waited=$(( waited + ${HEALTH_POLL:-20} ))
done
log_ok "revision $REVISION healthy"
if $RESTORED_DOMAINS; then
  # The bindings live on the app again; an old save must not resurrect them later.
  print -rn -- '[]' | kv_secret_set_from_stdin "$SERVER-custom-domains"
  log_ok "cleared $SERVER-custom-domains (bindings restored)"
fi

if [[ "$(jqs .easy_auth)" != true ]] && \
   [[ "$(az containerapp auth show -g "$RG" -n "$APP_NAME" --query platform.enabled -o tsv 2>/dev/null || true)" == true ]]; then
  az containerapp auth update -g "$RG" -n "$APP_NAME" --enabled false --output none
  log_ok "Easy Auth disabled (easy_auth: false)"
fi

log_step "6/6 Realms"
if $SKIP_REALMS; then
  log_warn "--skip-realms: not reconciling realms"
else
  for row in "${(@f)$(jq -c '.realms[]' "$SERVER_JSON")}"; do
    slug="$(print -r -- "$row" | jq -r '.slug')"
    label="${slug:-(root)}"
    if [[ "$(print -r -- "$row" | jq -r .active)" != true ]]; then
      gh_warning "realm $label on $SERVER is marked inactive in chat.yml; deactivate it deliberately with realm.zsh --deactivate $slug"
      continue
    fi
    res="$(RUN_JOB_EXPECT=".slug == \"$slug\" and has(\"created\")" run_job "$MGMT_JOB" mgmt chat:manage ensure-realm "${slug:-_root}" \
      "$(print -r -- "$row" | jq -r .name)" "$(print -r -- "$row" | jq -r .owner.email)" \
      "$(print -r -- "$row" | jq -r .owner.name)")"
    log_ok "realm $label: $(print -r -- "$res" | jq -r 'if .created then "created" else "present" end') $(print -r -- "$res" | jq -r .url)"
  done
fi

if ! hc_enabled && az containerapp job show -g "$RG" -n "$(jqs .jobs.hc)" >/dev/null 2>&1; then
  az_retry containerapp job delete -g "$RG" -n "$(jqs .jobs.hc)" --yes --output none
  log_ok "healthchecks disabled: deleted $(jqs .jobs.hc)"
fi
if hc_enabled; then
  log_step "Healthchecks"
  if [[ -n "${HEALTHCHECKS_API_KEY:-}" ]] || kv_secret_exists healthchecks-api-key; then
    "${0:A:h}/healthchecks.zsh" --config "$CONFIG_DIR" --sync
  else
    log_info "no healthchecks-api-key: checks are created on first ping with default schedules"
  fi
  log_ok "$(jqs .jobs.hc) pings $(jqs .healthchecks.health) every $(jqp .healthchecks.health_interval_minutes) min"
fi

log_step "Where it is reachable"
"$CHAT_PY" "$CHAT_RENDER" urls "$CONFIG_DIR" --default-domain "$DOMAIN" | grep -E "^$SERVER " >&2 || true
summary "### $SERVER deployed"
summary "- revision \`$REVISION\`, image \`$IMAGE\`"
summary "- app FQDN (works before DNS): https://$FQDN/"
if [[ "$(jqs .dns)" == pending ]]; then
  summary "- dns: **pending** — when the records from \`bind-domain.zsh --print\` exist, bind them and set \`dns: live\`"
fi
