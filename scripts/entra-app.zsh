#!/usr/bin/env zsh
# entra-app.zsh — the Entra app registration Zulip signs people in with (OIDC), one per
# server. Run by an operator with rights to manage app registrations; CI never touches
# Entra.
#
#   scripts/entra-app.zsh --config <dept-dir> --server <name> [--group <object-id>]
#                         [--rotate-secret] [--prune-old-secrets] [--prune-redirects] [--yes]
#
# Idempotent. Each run:
#   * creates <prefix>-<server>-zulip if missing (single tenant);
#   * sets redirect URIs for where the server answers NOW and LATER — the pre-DNS
#     *.azurecontainerapps.io name (while dns: pending) and the live callback
#     (https://<host>/complete/oidc/, or https://auth.<external_host>/complete/oidc/ on a
#     shared server). Existing URIs are kept unless --prune-redirects;
#   * requests the email claim in ID tokens;
#   * requires user assignment on the enterprise app and, with --group or
#     entra.allowed_group_id, assigns that group — so only its members can sign in;
#   * creates a client secret straight into Key Vault (<server>-oidc-secret) if there is
#     none, or --rotate-secret; the value is never printed;
#   * with --prune-old-secrets (after the servers restarted on a rotated secret): deletes
#     every zulip-oidc-* credential but the newest, so a superseded secret stops working;
#   * stores the client id in Key Vault (<server>-oidc-client-id) for deploys, and prints
#     it so you can pin it as servers.<name>.entra.client_id in chat.yml.
source "${0:A:h}/common.zsh"
GROUP="" ROTATE=false PRUNE=false PRUNE_SECRETS=false
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --group) GROUP="${2:?}"; shift 2 ;;
    --rotate-secret) ROTATE=true; shift ;;
    --prune-redirects) PRUNE=true; shift ;;
    --prune-old-secrets) PRUNE_SECRETS=true; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
load_platform
load_server
az_login

DISPLAY_NAME="$PREFIX-$SERVER-zulip"
GROUP="${GROUP:-$(jq -r '.entra.allowed_group_id // empty' "$SERVER_JSON")}"
EASY_AUTH="$(jqs .easy_auth)"

log_step "App registration $DISPLAY_NAME"
# Only an app we own: a look-alike would receive this server's sign-in client secret.
APP_ID="$(entra_app_by_name "$DISPLAY_NAME")" || die "not using app registration $DISPLAY_NAME"
if [[ -z "$APP_ID" ]]; then
  confirm "Create Entra app registration $DISPLAY_NAME?" || die "aborted"
  APP_ID="$(az ad app create --display-name "$DISPLAY_NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)"
  log_ok "created ($APP_ID)"
else
  log_ok "exists ($APP_ID)"
fi
PINNED="$(jq -r '.entra.client_id // empty' "$SERVER_JSON")"
[[ -z "$PINNED" || "$PINNED" == "$APP_ID" ]] || die "chat.yml pins entra.client_id=$PINNED but $DISPLAY_NAME is $APP_ID"

log_step "Redirect URIs"
typeset -aU WANT
WANT=("$(jqs .oidc_callback_live)")
DOMAIN="$(env_default_domain 2>/dev/null || true)"
if [[ -n "$DOMAIN" ]]; then
  WANT+=("https://$APP_NAME.$DOMAIN/complete/oidc/")
else
  log_warn "platform not deployed yet; the pre-DNS redirect URI will be added on the next run"
fi
if [[ "$EASY_AUTH" == true ]]; then
  for h in "${(@f)$(jqs '.hosts[]')}"; do WANT+=("https://$h/.auth/login/aad/callback"); done
  [[ -n "$DOMAIN" ]] && WANT+=("https://$APP_NAME.$DOMAIN/.auth/login/aad/callback")
fi
typeset -aU HAVE
HAVE=("${(@f)$(az ad app show --id "$APP_ID" --query 'web.redirectUris[]' -o tsv)}")
HAVE=("${(@)HAVE:#}")
if $PRUNE; then FINAL=("${WANT[@]}"); else FINAL=("${HAVE[@]}" "${WANT[@]}"); fi
typeset -U FINAL
if [[ "${(j:\n:)${(o)FINAL}}" == "${(j:\n:)${(o)HAVE}}" ]]; then
  log_ok "unchanged (${#FINAL})"
else
  az ad app update --id "$APP_ID" --web-redirect-uris "${FINAL[@]}" \
    --enable-id-token-issuance "$EASY_AUTH" --output none
  log_ok "set ${#FINAL} redirect URI(s)"
fi
for u in "${FINAL[@]}"; do print -u2 -r -- "    $u"; done

log_step "Claims and assignment"
CLAIMS="$(mktemp)"
print -r -- '{"idToken":[{"name":"email","essential":true}],"accessToken":[],"saml2Token":[]}' > "$CLAIMS"
az ad app update --id "$APP_ID" --optional-claims "@$CLAIMS" --output none
rm -f "$CLAIMS"
az ad sp show --id "$APP_ID" >/dev/null 2>&1 || az ad sp create --id "$APP_ID" --output none
SP_ID="$(az ad sp show --id "$APP_ID" --query id -o tsv)"
az ad sp update --id "$SP_ID" --set appRoleAssignmentRequired=true --output none
log_ok "user assignment required on the enterprise app"
if [[ -n "$GROUP" ]]; then
  if az rest --method get --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
      --query "value[?principalId=='$GROUP'].id" -o tsv | grep -q .; then
    log_ok "group $GROUP already assigned"
  else
    BODY="$(mktemp)"
    jq -n --arg p "$GROUP" --arg r "$SP_ID" '{principalId: $p, resourceId: $r, appRoleId: "00000000-0000-0000-0000-000000000000"}' > "$BODY"
    az rest --method post --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
      --headers Content-Type=application/json --body "@$BODY" --output none
    rm -f "$BODY"
    log_ok "assigned group $GROUP"
  fi
else
  gh_warning "no Entra group assigned to $DISPLAY_NAME: nobody can sign in until users or a group are assigned (--group <object-id>)"
fi

log_step "Client secret and id in Key Vault"
if kv_secret_exists "$SERVER-oidc-secret" && ! $ROTATE; then
  log_ok "$SERVER-oidc-secret present"
else
  # Record the real expiry on the vault secret: keepalive.zsh warns before it lapses
  # (CI can read vault metadata, but has no Entra rights to read the app itself).
  CRED="$(az ad app credential reset --id "$APP_ID" --append --display-name "zulip-oidc-$(date -u +%Y%m%d)" \
    --years 2 --query '{password: password, end: endDateTime}' -o json)"
  END="$(jq -er .end <<<"$CRED" 2>/dev/null || "$CHAT_PY" -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
  jq -jr .password <<<"$CRED" | kv_secret_set_from_stdin "$SERVER-oidc-secret" --expires "$END"
  unset CRED
  log_ok "new client secret stored as $SERVER-oidc-secret (expires $END; rerun with --rotate-secret)"
fi
if [[ "$(kv_secret_get "$SERVER-oidc-client-id" 2>/dev/null || true)" != "$APP_ID" ]]; then
  print -rn -- "$APP_ID" | kv_secret_set_from_stdin "$SERVER-oidc-client-id"
fi
log_ok "client id $APP_ID — pin it in chat.yml: servers.$SERVER.entra.client_id: $APP_ID"
if $PRUNE_SECRETS; then
  old=("${(@f)$(az ad app credential list --id "$APP_ID" -o json \
    | jq -r '[.[] | select((.displayName // .customKeyIdentifier // "") | tostring | startswith("zulip-oidc"))] | sort_by(.endDateTime) | .[:-1][] | .keyId')}")
  for k in "${old[@]}"; do
    [[ -n "$k" ]] || continue
    az ad app credential delete --id "$APP_ID" --key-id "$k"
    log_ok "deleted superseded client secret $k"
  done
  (( ${#old} )) && [[ -n "${old[1]}" ]] || log_info "no superseded client secrets"
fi
