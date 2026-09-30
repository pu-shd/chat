#!/usr/bin/env zsh
# healthchecks.zsh — Healthchecks.io checks for a department (optional; healthchecks.enabled).
#
#   scripts/healthchecks.zsh --config <dept> --sync            create/update every check
#   scripts/healthchecks.zsh --config <dept> --list            what exists, with status
#   scripts/healthchecks.zsh --config <dept> --ping <slug> [--fail|--start] [--message M]
#   scripts/healthchecks.zsh --config <dept> --delete [--server <name>]   (type-to-confirm)
#
# Checks and their schedules come from generated/platform.json (render.py):
#   <prefix>-<server>-health    the in-environment -hc job, every few minutes
#   <prefix>-<server>-web       daily keepalive from GitHub Actions
#   <prefix>-<server>-ip-gate   daily IP gate refresh (gated servers)
#   <prefix>-updates            weekly update check in the config repo
# Pings use the project ping key + slug (https://hc-ping.com/<key>/<slug>), so no
# per-check URL has to be stored anywhere. --sync needs the project's API key
# (HEALTHCHECKS_API_KEY, or Key Vault healthchecks-api-key); without it checks are
# created on first ping with Healthchecks' default schedule, which you then adjust.
source "${0:A:h}/common.zsh"
ACTION="" SLUG="" KIND=success MSG=""
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --sync) ACTION=sync; shift ;;
    --list) ACTION=list; shift ;;
    --delete) ACTION=delete; shift ;;
    --ping) ACTION=ping; SLUG="${2:?--ping needs a slug}"; shift 2 ;;
    --fail) KIND=fail; shift ;;
    --start) KIND=start; shift ;;
    --message) MSG="${2:?}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$ACTION" ]] || die "one of --sync, --list, --ping, --delete"
load_platform
hc_enabled || { log_info "healthchecks.enabled is false in chat.yml; nothing to do"; exit 0; }
API="$(jqp .healthchecks.api_base)"

if [[ "$ACTION" == ping ]]; then
  jq -e --arg s "$SLUG" '.healthchecks.checks | any(.slug == $s)' "$PLATFORM_JSON" >/dev/null \
    || die "no check '$SLUG' in generated/platform.json"
  [[ -n "${HEALTHCHECKS_PING_KEY:-}" ]] || az_login
  hc_ping "$SLUG" "$KIND" "$MSG"
  exit 0
fi

api_key() {
  if [[ -n "${HEALTHCHECKS_API_KEY:-}" ]]; then print -rn -- "$HEALTHCHECKS_API_KEY"; return 0; fi
  az_login >/dev/null
  az keyvault secret show --vault-name "$KV_NAME" --name healthchecks-api-key --query value -o tsv 2>/dev/null
}
KEY="$(api_key || true)"
[[ -n "$KEY" ]] || die "no Healthchecks API key (HEALTHCHECKS_API_KEY or Key Vault healthchecks-api-key)"
HDR="$(mktemp)"; chmod 600 "$HDR"
trap 'rm -f "$HDR"' EXIT
print -r -- "X-Api-Key: $KEY" > "$HDR"   # header from a file: the key never reaches argv

hc_api() {  # hc_api <method> <path-or-url> [json-body]
  local url="$2"
  [[ "$url" == https://* ]] || url="$API$2"
  if (( $# > 2 )); then
    curl -fsS -m 30 -X "$1" -H "@$HDR" -H 'Content-Type: application/json' --data-raw "$3" "$url"
  else
    curl -fsS -m 30 -X "$1" -H "@$HDR" "$url"
  fi
}
existing() { hc_api GET "/checks/?slug=$1" | jq -c '.checks[0] // empty'; }

case "$ACTION" in
  sync)
    n=0
    for c in "${(@f)$(jq -c '.healthchecks.checks[]' "$PLATFORM_JSON")}"; do
      slug="$(jq -r .slug <<<"$c")"
      body="$(jq -c '{name, slug, desc, timeout, grace, tags: (.tags | join(" "))}' <<<"$c")"
      cur="$(existing "$slug")"
      if [[ -n "$cur" ]]; then
        hc_api POST "$(jq -r .update_url <<<"$cur")" "$body" >/dev/null
        log_ok "updated $slug"
      else
        hc_api POST /checks/ "$body" >/dev/null
        log_ok "created $slug"
      fi
      n=$(( n + 1 ))
    done
    (( n > 0 )) || die "no checks rendered; is healthchecks.enabled true and generated/ current?"
    summary "- healthchecks: $n check(s) synced"
    ;;
  list)
    for c in "${(@f)$(jq -r '.healthchecks.checks[].slug' "$PLATFORM_JSON")}"; do
      cur="$(existing "$c")"
      if [[ -n "$cur" ]]; then print -r -- "$c	$(jq -r '.status' <<<"$cur")	last ping $(jq -r '.last_ping // "never"' <<<"$cur")"
      else print -r -- "$c	MISSING (run --sync)"; fi
    done
    ;;
  delete)
    typeset -a slugs
    if [[ -n "$SERVER" ]]; then
      slugs=("${(@f)$(jq -r --arg p "$(jqp .prefix)-$SERVER-" '.healthchecks.checks[].slug | select(startswith($p))' "$PLATFORM_JSON")}")
      confirm_typed "DELETE-CHECKS $SERVER"
    else
      slugs=("${(@f)$(jq -r '.healthchecks.checks[].slug' "$PLATFORM_JSON")}")
      confirm_typed "DELETE-CHECKS $(jqp .department)"
    fi
    for slug in "${slugs[@]}"; do
      [[ -n "$slug" ]] || continue
      cur="$(existing "$slug")"
      if [[ -n "$cur" ]]; then hc_api DELETE "/checks/$(jq -r '.uuid // (.update_url | split("/") | last)' <<<"$cur")" >/dev/null; log_ok "deleted $slug"
      else log_info "$slug not present"; fi
    done
    ;;
esac
