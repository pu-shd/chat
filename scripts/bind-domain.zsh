#!/usr/bin/env zsh
# bind-domain.zsh — custom hostnames for one server, before and after the DNS records exist.
#
#   scripts/bind-domain.zsh --config <dept-dir> --server <name> --print
#       The DNS request (CNAME + asuid TXT per hostname), as Markdown. Works as
#       soon as the platform exists — no app or DNS needed — so the ticket can go in
#       while you test on the *.azurecontainerapps.io name.
#   scripts/bind-domain.zsh --config <dept-dir> --server <name> [--wait MINUTES] [--host H]
#       Check the records resolve, then add + bind each hostname with a certificate:
#       an ACA managed certificate, or cert.key_vault_certificate from Key Vault.
#
# After binding: set dns: live for the server in chat.yml, run entra-app.zsh (adds the
# live callback) and let CI redeploy — that switches Zulip's EXTERNAL_HOST.
source "${0:A:h}/common.zsh"
PRINT=false WAIT=0 ONLY_HOST="" FORCE=false
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --print) PRINT=true; shift ;;
    --wait) WAIT="${2:?}"; shift 2 ;;
    --host) ONLY_HOST="${2:?}"; shift 2 ;;
    --force) FORCE=true; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
load_platform
load_server
require_cmd dig
az_login

DOMAIN="$(env_default_domain)"
VERIFY_ID="$(env_verification_id)"
[[ -n "$DOMAIN" && -n "$VERIFY_ID" ]] || die "cannot read $ENV_NAME's domain/verification id; deploy the platform first"
TARGET="$APP_NAME.$DOMAIN"
typeset -a HOSTS
HOSTS=("${(@f)$(jqs '.hosts[]')}")
[[ -n "$ONLY_HOST" ]] && HOSTS=("${(@M)HOSTS:#$ONLY_HOST}")
(( ${#HOSTS} )) || die "no matching hostnames for $SERVER"

ticket() {
  print -r -- "### DNS request: $SERVER ($(jqp .department) chat)"
  print -r -- ""
  print -r -- "Please create these records. Each CNAME points at an Azure Container App; the TXT"
  print -r -- "records prove domain ownership to Azure so it can issue the TLS certificate."
  print -r -- ""
  print -r -- "| Type | Name | Value | TTL |"
  print -r -- "|---|---|---|---|"
  local h
  for h in "${HOSTS[@]}"; do
    print -r -- "| CNAME | \`$h\` | \`$TARGET\` | 3600 |"
    print -r -- "| TXT | \`asuid.$h\` | \`$VERIFY_ID\` | 3600 |"
  done
  print -r -- ""
  print -r -- "Until these exist the service is reachable at https://$TARGET/ ."
}

if $PRINT; then
  ticket
  summary "$(ticket)"
  exit 0
fi

app_exists || die "$APP_NAME is not deployed yet; run deploy-server.zsh first (it works without DNS)"
CERT="$(jq -r '.cert.key_vault_certificate // empty' "$SERVER_JSON")"
if [[ -z "$CERT" && "$(jqs .ip_gate)" == true ]] && ! $FORCE; then
  die "$SERVER has ip_gate on: DigiCert cannot reach it to issue or renew a managed certificate. Set cert.key_vault_certificate (or --force)"
fi

dns_ready() {
  local h="$1" cname txt
  cname="$(dig +short CNAME "$h" | sed 's/\.$//' | tr 'A-Z' 'a-z')"
  txt="$(dig +short TXT "asuid.$h" | tr -d '"')"
  [[ "$cname" == "${TARGET:l}" && "$txt" == *"$VERIFY_ID"* ]]
}

deadline=$(( $(date +%s) + WAIT * 60 ))
typeset -a missing
while true; do
  missing=()
  for h in "${HOSTS[@]}"; do dns_ready "$h" || missing+=("$h"); done
  (( ${#missing} == 0 )) && break
  if (( $(date +%s) >= deadline )); then
    log_error "DNS not in place yet for: ${missing[*]}"
    print -u2 -r -- "Expected for each: CNAME → $TARGET and TXT asuid.<host> → $VERIFY_ID"
    print -u2 -r -- "Print the DNS request with: $0 --config $CONFIG_DIR --server $SERVER --print"
    exit 4
  fi
  log_info "waiting for DNS: ${missing[*]}"
  sleep 60
done
log_ok "DNS records resolve for ${#HOSTS} hostname(s)"

if [[ -n "$CERT" ]]; then
  # The environment's identity may read this certificate and nothing else in the vault.
  ENV_PID="$(az identity show -g "$RG" -n "$ID_NAME" --query principalId -o tsv)"
  CERT_SCOPE="$(az keyvault show -n "$KV_NAME" -g "$RG" --query id -o tsv)/secrets/$CERT"
  if [[ -z "$(az role assignment list --assignee "$ENV_PID" --scope "$CERT_SCOPE" --role 'Key Vault Secrets User' --query '[0].id' -o tsv 2>/dev/null)" ]]; then
    az role assignment create --assignee-object-id "$ENV_PID" --assignee-principal-type ServicePrincipal \
      --role 'Key Vault Secrets User' --scope "$CERT_SCOPE" --output none
    log_ok "granted $ID_NAME read on certificate $CERT"
  fi
  if ! az containerapp env certificate list -g "$RG" -n "$ENV_NAME" --query "[?name=='$CERT'].name" -o tsv | grep -qx "$CERT"; then
    az_retry containerapp env certificate upload -g "$RG" -n "$ENV_NAME" --certificate-name "$CERT" \
      --akv-url "https://$KV_NAME.vault.azure.net/secrets/$CERT" \
      --identity "$(az identity show -g "$RG" -n "$ID_NAME" --query id -o tsv)" --output none
    log_ok "imported Key Vault certificate $CERT into $ENV_NAME"
  fi
fi

BOUND="$(az containerapp hostname list -g "$RG" -n "$APP_NAME" --query "[?bindingType=='SniEnabled'].name" -o tsv 2>/dev/null || true)"
for h in "${HOSTS[@]}"; do
  if print -r -- "$BOUND" | grep -qx "$h"; then
    log_ok "$h already bound"
    continue
  fi
  if [[ -n "$CERT" ]]; then
    az_retry containerapp hostname bind -g "$RG" -n "$APP_NAME" --hostname "$h" \
      --environment "$ENV_NAME" --certificate "$CERT" --output none
  else
    az_retry containerapp hostname bind -g "$RG" -n "$APP_NAME" --hostname "$h" \
      --environment "$ENV_NAME" --validation-method CNAME --output none
  fi
  log_ok "bound $h"
  summary "- bound \`$h\` → $APP_NAME"
done
print -u2 -r -- ""
print -u2 -r -- "Next: set servers.$SERVER.dns: live in chat.yml, run entra-app.zsh --server $SERVER, commit and push."
