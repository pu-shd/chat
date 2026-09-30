#!/usr/bin/env zsh
# acs-email.zsh — Azure Communication Services Email for a department (email.provider: acs).
# Operator-run (Entra app and a role assignment, which CI cannot create).
#
#   scripts/acs-email.zsh --config <dept> [--print] [--verify [--wait MINUTES]]
#                         [--rotate-secret] [--prune-old-secrets] [--yes]
#
# The platform (deploy-platform.zsh) creates the Email service, the domain, its sender
# usernames and the Communication Services resource. This script:
#   * custom domain: --print writes the DNS records for OIT (domain TXT, SPF, DKIM x2,
#     plus a recommended DMARC); --verify asks Azure to check them and, once all are
#     verified, links the domain to the Communication Services resource. Mail cannot be
#     sent from a custom domain before that;
#   * creates the Entra app <prefix>-acs-smtp that authenticates SMTP, grants it the
#     role ACS needs on the Communication Services resource, and stores its client secret
#     in Key Vault as email-password (same name as for Resend, with its expiry recorded);
#   * creates the SMTP username <prefix>-smtp linked to that app — the SMTP login Zulip
#     uses (smtp.azurecomm.net:587, STARTTLS).
# Idempotent. Then deploy, and check with realm.zsh --send-test-email you@princeton.edu.
source "${0:A:h}/common.zsh"
PRINT=false VERIFY=false WAIT=0 ROTATE=false PRUNE_SECRETS=false
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --print) PRINT=true; shift ;;
    --verify) VERIFY=true; shift ;;
    --wait) WAIT="${2:?}"; shift 2 ;;
    --rotate-secret) ROTATE=true; shift ;;
    --prune-old-secrets) PRUNE_SECRETS=true; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
load_platform
[[ "$(jqp .email.provider)" == acs ]] || die "email.provider is not acs in chat.yml"
az_login

ES="$(jqp .acs.email_service)"
CS="$(jqp .acs.communication_service)"
DOMAIN="$(jqp .acs.domain)"
MANAGED="$(jqp .acs.managed)"
SMTP_USER="$(jqp .email.user)"
APP_DISPLAY="$(jqp .acs.entra_app)"
# ACS SMTP needs the app to hold this role on the Communication Services resource
# (override with CHAT_ACS_ROLE if your tenant uses a custom role).
ROLE="${CHAT_ACS_ROLE:-Communication and Email Service Owner}"

az communication show -g "$RG" -n "$CS" --query id -o tsv >/dev/null 2>&1 \
  || die "Communication Services $CS not found; run deploy-platform.zsh first"
CS_ID="$(az communication show -g "$RG" -n "$CS" --query id -o tsv)"
DOMAIN_ID="$(az communication email domain show -g "$RG" --email-service-name "$ES" -n "$DOMAIN" --query id -o tsv)"

domain_json() { az communication email domain show -g "$RG" --email-service-name "$ES" -n "$DOMAIN" -o json; }

dns_ticket() {
  local d
  d="$(domain_json)"
  print -r -- "### DNS request: email for $DOMAIN (Azure Communication Services)"
  print -r -- ""
  print -r -- "| Type | Name | Value | TTL |"
  print -r -- "|---|---|---|---|"
  print -r -- "$d" | jq -r '.verificationRecords | to_entries[] | select(.value != null)
    | "| \(.value.type) | `\(.value.name)` | `\(.value.value)` | \(.value.ttl // 3600) |"'
  print -r -- "| TXT | \`_dmarc.$DOMAIN\` | \`v=DMARC1; p=quarantine; rua=mailto:$(jqp .admin_email)\` | 3600 | (recommended)"
  print -r -- ""
  print -r -- "SPF: if $DOMAIN already has an SPF TXT record, merge \`include:spf.protection.outlook.com\` into it rather than adding a second one."
}

if [[ "$MANAGED" != true ]]; then
  log_step "Custom domain $DOMAIN"
  if $PRINT; then
    dns_ticket
    summary "$(dns_ticket)"
    exit 0
  fi
  states="$(domain_json | jq -c '.verificationStates // {}')"
  unverified=("${(@f)$(print -r -- "$states" | jq -r 'to_entries[] | select(.key != "DMARC" and .value.status != "Verified") | .key')}")
  unverified=("${(@)unverified:#}")
  if (( ${#unverified} )) && $VERIFY; then
    for t in "${unverified[@]}"; do
      az communication email domain initiate-verification -g "$RG" --email-service-name "$ES" \
        --domain-name "$DOMAIN" --verification-type "$t" --output none || log_warn "could not start $t verification"
    done
    deadline=$(( $(date +%s) + WAIT * 60 ))
    while true; do
      states="$(domain_json | jq -c '.verificationStates // {}')"
      unverified=("${(@f)$(print -r -- "$states" | jq -r 'to_entries[] | select(.key != "DMARC" and .value.status != "Verified") | .key')}")
      unverified=("${(@)unverified:#}")
      (( ${#unverified} == 0 )) && break
      (( $(date +%s) >= deadline )) && break
      log_info "waiting for verification: ${unverified[*]}"
      sleep 60
    done
  fi
  if (( ${#unverified} )); then
    gh_warning "email: $DOMAIN not verified yet (${unverified[*]}); mail cannot be sent from it. Send OIT the records from --print, then rerun with --verify"
  else
    log_ok "$DOMAIN verified"
    linked="$(az communication show -g "$RG" -n "$CS" --query 'linkedDomains' -o json)"
    if ! print -r -- "$linked" | jq -e --arg id "$DOMAIN_ID" 'any(.[]?; ascii_downcase == ($id | ascii_downcase))' >/dev/null; then
      az_retry communication update -g "$RG" -n "$CS" --linked-domains "[\"$DOMAIN_ID\"]" --output none
      log_ok "linked $DOMAIN to $CS"
    else
      log_ok "$DOMAIN already linked"
    fi
  fi
else
  log_ok "Azure-managed domain: sender $(domain_json | jq -r '"DoNotReply@" + .mailFromSenderDomain')"
fi

log_step "Entra app $APP_DISPLAY (SMTP authentication)"
APP_ID="$(az ad app list --display-name "$APP_DISPLAY" --query '[0].appId' -o tsv)"
if [[ -z "$APP_ID" ]]; then
  confirm "Create Entra app registration $APP_DISPLAY for ACS SMTP?" || die "aborted"
  APP_ID="$(az ad app create --display-name "$APP_DISPLAY" --sign-in-audience AzureADMyOrg --query appId -o tsv)"
  log_ok "created ($APP_ID)"
else
  log_ok "exists ($APP_ID)"
fi
az ad sp show --id "$APP_ID" >/dev/null 2>&1 || az ad sp create --id "$APP_ID" --output none
SP_ID="$(az ad sp show --id "$APP_ID" --query id -o tsv)"

if [[ -z "$(az role assignment list --assignee "$SP_ID" --scope "$CS_ID" --role "$ROLE" --query '[0].id' -o tsv 2>/dev/null)" ]]; then
  for i in {1..6}; do
    az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
      --role "$ROLE" --scope "$CS_ID" --output none 2>/dev/null && break
    if (( i == 6 )); then die "could not grant '$ROLE' on $CS"; fi
    sleep 10
  done
  log_ok "granted '$ROLE' on $CS"
else
  log_ok "'$ROLE' already granted"
fi

if kv_secret_exists email-password && ! $ROTATE; then
  log_ok "email-password present"
else
  CRED="$(az ad app credential reset --id "$APP_ID" --append --display-name "zulip-smtp-$(date -u +%Y%m%d)" \
    --years 2 --query '{password: password, end: endDateTime}' -o json)"
  END="$(jq -er .end <<<"$CRED" 2>/dev/null || "$CHAT_PY" -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
  jq -jr .password <<<"$CRED" | kv_secret_set_from_stdin email-password --expires "$END"
  unset CRED
  log_ok "SMTP client secret stored as email-password (expires $END); servers pick it up on restart"
fi
if $PRUNE_SECRETS; then
  old=("${(@f)$(az ad app credential list --id "$APP_ID" -o json \
    | jq -r '[.[] | select((.displayName // "") | startswith("zulip-smtp"))] | sort_by(.endDateTime) | .[:-1][] | .keyId')}")
  for k in "${old[@]}"; do
    [[ -n "$k" ]] || continue
    az ad app credential delete --id "$APP_ID" --key-id "$k"
    log_ok "deleted superseded SMTP secret $k"
  done
fi

log_step "SMTP username $SMTP_USER"
if az communication smtp-username show -g "$RG" --comm-service-name "$CS" -n "$SMTP_USER" >/dev/null 2>&1; then
  log_ok "exists"
else
  az_retry communication smtp-username create -g "$RG" --comm-service-name "$CS" -n "$SMTP_USER" \
    --username "$SMTP_USER" --entra-application-id "$APP_ID" --tenant-id "$TENANT_ID" --output none
  log_ok "created"
fi
summary "- email: ACS \`$CS\`, SMTP user \`$SMTP_USER\`, domain \`$DOMAIN\`"
log_info "next: deploy (or update-server.zsh --restart), then realm.zsh --send-test-email you@princeton.edu"
