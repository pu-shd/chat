#!/usr/bin/env zsh
# smoke.zsh — end-to-end checks against a deployed server, over the public internet.
#
#   scripts/smoke.zsh --config <dept-dir> --server <name> [--runner-ip auto|<ip>]
#
# For every realm reachable right now (the *.azurecontainerapps.io name while
# dns: pending, the real hostnames once live):
#   * GET /api/v1/server_settings returns 200 with that realm's URL;
#   * "Log in with Entra" redirects to login.microsoftonline.com/<tenant>/;
# and on the redirect host, every chat.<dept>/<slug> answers 302 to its target.
# With ip_gate on, --runner-ip adds a temporary allow rule and always removes it.
source "${0:A:h}/common.zsh"
RUNNER_IP=""
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --runner-ip) RUNNER_IP="${2:?}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
load_platform
load_server
require_cmd curl
az_login
DOMAIN="$(env_default_domain)"
GATE="${0:A:h}/ip-gate.zsh"

if [[ "$(jqs .ip_gate)" == true && -n "$RUNNER_IP" ]]; then
  [[ "$RUNNER_IP" == auto ]] && RUNNER_IP="$(curl -fsS --max-time 10 https://api.ipify.org)"
  "$GATE" --config "$CONFIG_DIR" --server "$SERVER" --add-temp "$RUNNER_IP"
  trap '"$GATE" --config "$CONFIG_DIR" --server "$SERVER" --remove-temp || log_error "could not remove the temporary IP rule; remove ci-runner-temp by hand"' EXIT
  sleep 20
fi

fails=0
check() {  # check <description> <command...>
  if "${@:2}"; then log_ok "$1"; else log_error "$1"; fails=$(( fails + 1 )); fi
}
realm_ok() {
  local url="$1" body
  body="$(curl -fsS --max-time 30 "${url}api/v1/server_settings")" || return 1
  print -r -- "$body" | jq -e --arg u "${url%/}" '.result == "success" and (.realm_url // .realm_uri) == $u' >/dev/null
}
oidc_ok() {
  local url="$1" loc
  # Follow redirects: a live shared server bounces through auth.<external_host> first.
  loc="$(curl -sS -L --max-redirs 5 -o /dev/null --max-time 30 -w '%{url_effective}' "${url}accounts/login/social/oidc/entra")"
  [[ "$loc" == https://login.microsoftonline.com/$TENANT_ID/* ]]
}
redirect_ok() {
  local from="$1" want="$2" got code
  got="$(curl -sS -o /dev/null --max-time 30 -w '%{http_code} %{redirect_url}' "$from")"
  code="${got%% *}"; got="${got#* }"
  [[ "$code" == 302 && "$got" == "$want" ]]
}

typeset -a ROWS
ROWS=("${(@f)$("$CHAT_PY" "$CHAT_RENDER" urls "$CONFIG_DIR" --default-domain "$DOMAIN" --json | jq -c --arg s "$SERVER" '.[] | select(.server == $s)')}")
tested=0
for row in "${ROWS[@]}"; do
  url="$(jq -r .now <<<"$row")"
  [[ "$url" == https://* ]] || { log_info "skip $(jq -r .realm <<<"$row"): $url"; continue; }
  check "realm $(jq -r .realm <<<"$row") answers at $url" realm_ok "$url"
  check "Entra sign-in redirect from $url" oidc_ok "$url"
  tested=$(( tested + 1 ))
done
(( tested > 0 )) || die "no realm of $SERVER is reachable yet; nothing was tested"

REDIRECT_HOST="$(jq -r '.redirect_host // empty' "$CONFIG_DIR/generated/index.json")"
if [[ "$REDIRECT_HOST" == "$SERVER" ]]; then
  base="$("$CHAT_PY" "$CHAT_RENDER" urls "$CONFIG_DIR" --default-domain "$DOMAIN" --json | jq -r --arg s "$SERVER" '.[] | select(.server == $s) | .now')"
  urls_json="$("$CHAT_PY" "$CHAT_RENDER" urls "$CONFIG_DIR" --default-domain "$DOMAIN" --json)"
  for r in "${(@f)$(jq -c '.redirects[]' "$SERVER_JSON")}"; do
    [[ -n "$r" ]] || continue
    slug="$(jq -r .slug <<<"$r")"
    want="$(jq -r --arg s "$(jq -r .server <<<"$r")" --arg m "$(jq -r '.realm | if . == "" then "(root)" else . end' <<<"$r")" \
      '.[] | select(.server == $s and .realm == $m) | .now' <<<"$urls_json")"
    if [[ "$want" != https://* ]]; then log_info "skip /$slug: target not reachable before DNS"; continue; fi
    check "${base}$slug → $want" redirect_ok "${base}$slug" "$want"
  done
fi

summary "- smoke $SERVER: $tested realm(s) checked, $fails failure(s)"
(( fails == 0 )) || die "$fails smoke check(s) failed"
log_ok "smoke tests passed"
