#!/usr/bin/env zsh
# ip-gate.zsh — optional campus/VPN allowlist for one Zulip server, from pugwips.
#
#   ip-gate.zsh --config <dept> --server <name> --emit          print ipSecurityRestrictions JSON
#   ip-gate.zsh --config <dept> --server <name> --apply         write them to the live app
#   ip-gate.zsh --config <dept> --server <name> --add-temp <ip> allow one address (CI smoke test)
#   ip-gate.zsh --config <dept> --server <name> --remove-temp
#
# Where the VPN ranges come from, in order:
#   1. live   PUGWIPS_READ_TOKEN set: the signed gateways.json on pugwips' `latest` release,
#             signature checked with cosign against its resolve workflow.
#   2. static no token, or (1) failed: ip_gate.fallback_url if set, otherwise the dated
#             snapshot committed next to chat.yml. Warns when older than snapshot_max_age_days.
#   3. keep   neither worked: the pu-vpn-* rules already on the app, unchanged.
# Campus ranges are always included. The result is never narrower than prefix_mode asked
# for (pugwips' consumer contract): a snapshot in a different mode is rejected, not reused.
# If none of the three yields VPN ranges this exits non-zero rather than deploying an
# allowlist that silently locks VPN users out.
source "${0:A:h}/common.zsh"

MODE="" TEMP_IP=""
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --emit) MODE=emit; shift ;;
    --apply) MODE=apply; shift ;;
    --add-temp) MODE=add-temp; TEMP_IP="${2:?--add-temp needs an IPv4 address}"; shift 2 ;;
    --remove-temp) MODE=remove-temp; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$MODE" ]] || die "one of --emit, --apply, --add-temp, --remove-temp is required"

load_platform
load_server
[[ "$(jqs .ip_gate)" == true ]] || die "servers.$SERVER has ip_gate: false; nothing to do"

PUGWIPS_REPO="$(jqp .ip_gate.repo)"
PREFIX_MODE="$(jqp .ip_gate.prefix_mode)"
MAX_AGE="$(jqp .ip_gate.snapshot_max_age_days)"
SNAPSHOT="$CONFIG_DIR/$(jqp .ip_gate.snapshot)"
FALLBACK_URL="$(jq -r '.ip_gate.fallback_url // empty' "$PLATFORM_JSON")"
# pugwips signs with keyless cosign from its resolve workflow (scripts/verify.sh there).
SIG_IDENTITY_RE="${PUGWIPS_SIGNER_RE:-^https://github\\.com/${PUGWIPS_REPO//./\\.}/\\.github/workflows/resolve\\.yml@refs/heads/main\$}"
SIG_ISSUER="https://token.actions.githubusercontent.com"
TEMP_RULE="ci-runner-temp"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mode_rank() { case "$1" in exact) print 1 ;; slash24) print 2 ;; vendor) print 3 ;; *) print 0 ;; esac; }

# prefixes_from_gateways <file> — pugwips examples/read-prefixes.sh, same degradation:
# vendor -> vendor_prefixes, else cidrs, else derived /24; never down to exact.
prefixes_from_gateways() {
  jq -r --arg mode "$PREFIX_MODE" '
    def is_ipv4:
      test("^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$");
    def slash24: .ips[]? | select(is_ipv4) | sub("\\.[0-9]+$"; ".0/24");
    [ .gateways[]
      | if $mode == "exact" then .ips[]?
        elif $mode == "slash24" then (if has("cidrs") then .cidrs[]? else slash24 end)
        else (if has("vendor_prefixes") then .vendor_prefixes[]?
              elif has("cidrs") then .cidrs[]?
              else slash24 end)
        end
    ] | unique | .[]' "$1"
}

verify_signature() {
  local file="$1"
  if [[ ! -f "$file.sig" || ! -f "$file.pem" ]]; then
    log_warn "no .sig/.pem beside $(basename "$file")"
    return 1
  fi
  if ! command -v cosign >/dev/null 2>&1; then
    log_warn "cosign not installed; cannot verify $(basename "$file")"
    return 1
  fi
  cosign verify-blob --signature "$file.sig" --certificate "$file.pem" \
    --certificate-oidc-issuer "$SIG_ISSUER" --certificate-identity-regexp "$SIG_IDENTITY_RE" \
    "$file" >/dev/null 2>&1
}

SOURCE="" SOURCE_DETAIL=""
typeset -a VPN

try_live() {
  if [[ -z "${PUGWIPS_READ_TOKEN:-}" ]]; then
    log_info "PUGWIPS_READ_TOKEN not set; using the static fallback"
    return 1
  fi
  require_cmd gh
  local dir="$WORK/live"
  mkdir -p "$dir"
  if ! GH_TOKEN="$PUGWIPS_READ_TOKEN" gh release download latest --repo "$PUGWIPS_REPO" \
      --pattern 'gateways.json' --pattern 'gateways.json.sig' --pattern 'gateways.json.pem' \
      --dir "$dir" >/dev/null 2>&1; then
    gh_warning "ip-gate($SERVER): could not download gateways.json from $PUGWIPS_REPO; falling back"
    return 1
  fi
  if [[ "${PUGWIPS_ALLOW_UNSIGNED:-}" != 1 ]] && ! verify_signature "$dir/gateways.json"; then
    gh_warning "ip-gate($SERVER): gateways.json signature did not verify; not trusting it, falling back"
    return 1
  fi
  VPN=("${(@f)$(prefixes_from_gateways "$dir/gateways.json")}")
  (( ${#VPN} )) && [[ -n "${VPN[1]}" ]] || { gh_warning "ip-gate($SERVER): live gateways.json had no prefixes"; return 1; }
  SOURCE=live SOURCE_DETAIL="$PUGWIPS_REPO release latest"
}

try_static() {
  local file detail
  if [[ -n "$FALLBACK_URL" ]]; then
    file="$WORK/fallback.json"
    if ! curl -fsSL --max-time 30 "$FALLBACK_URL" -o "$file"; then
      gh_warning "ip-gate($SERVER): static link $FALLBACK_URL unreachable"
      return 1
    fi
    curl -fsSL --max-time 30 "$FALLBACK_URL.sig" -o "$file.sig" 2>/dev/null || rm -f "$file.sig"
    curl -fsSL --max-time 30 "$FALLBACK_URL.pem" -o "$file.pem" 2>/dev/null || rm -f "$file.pem"
    detail="$FALLBACK_URL"
  elif [[ -f "$SNAPSHOT" ]]; then
    file="$SNAPSHOT"
    detail="committed $(basename "$SNAPSHOT")"
  else
    gh_warning "ip-gate($SERVER): no static fallback (no fallback_url, no $SNAPSHOT)"
    return 1
  fi
  # A committed snapshot was reviewed in git. Anything fetched from a URL must prove it
  # came from pugwips: no signature, or a bad one, and it is not used.
  if [[ -n "$FALLBACK_URL" && "${PUGWIPS_ALLOW_UNSIGNED:-}" != 1 ]] && ! [[ -f "$file.sig" && -f "$file.pem" ]]; then
    gh_warning "ip-gate($SERVER): $detail has no .sig/.pem beside it; refusing an unsigned download"
    return 1
  fi
  if [[ -f "$file.sig" ]]; then
    verify_signature "$file" || { gh_warning "ip-gate($SERVER): $detail has a signature that does not verify; rejecting it"; return 1; }
    detail+=" (signature verified)"
  fi
  jq -e . "$file" >/dev/null 2>&1 || { gh_warning "ip-gate($SERVER): $detail is not JSON"; return 1; }

  local snap_date
  snap_date="$(jq -r '.snapshot_date // .resolved_at // empty' "$file" | cut -c1-10)"
  if [[ -n "$snap_date" ]]; then
    local age
    age="$(snapshot_age_days "$snap_date")" || { gh_warning "ip-gate($SERVER): unparseable snapshot_date '$snap_date'"; return 1; }
    if (( age > MAX_AGE )); then
      gh_warning "ip-gate($SERVER): static snapshot is $age days old (from $snap_date, limit $MAX_AGE); refresh it or provide PUGWIPS_READ_TOKEN"
    fi
    detail+=", snapshot $snap_date"
  else
    gh_warning "ip-gate($SERVER): $detail carries no snapshot_date; its age is unknown"
  fi

  if jq -e 'has("gateways")' "$file" >/dev/null; then
    VPN=("${(@f)$(prefixes_from_gateways "$file")}")
  else
    local snap_mode
    snap_mode="$(jq -r '.prefix_mode // empty' "$file")"
    if [[ "$snap_mode" != "$PREFIX_MODE" ]]; then
      if (( $(mode_rank "$snap_mode") < $(mode_rank "$PREFIX_MODE") )); then
        gh_warning "ip-gate($SERVER): snapshot is '$snap_mode' but prefix_mode is '$PREFIX_MODE'; refusing to narrow"
      else
        gh_warning "ip-gate($SERVER): snapshot is '$snap_mode' but prefix_mode is '$PREFIX_MODE'; refusing to widen beyond what was asked"
      fi
      return 1
    fi
    VPN=("${(@f)$(jq -r '.prefixes[]' "$file")}")
  fi
  (( ${#VPN} )) && [[ -n "${VPN[1]}" ]] || { gh_warning "ip-gate($SERVER): $detail had no prefixes"; return 1; }
  SOURCE=static SOURCE_DETAIL="$detail"
}

snapshot_age_days() {
  # Python, not date(1): GNU (CI) and BSD (macOS) date disagree, and BSD's -d is not a date.
  "$CHAT_PY" -c 'import sys, datetime as d; print((d.date.today() - d.date.fromisoformat(sys.argv[1])).days)' "$1"
}

live_rules() {
  local out
  out="$(az containerapp show -g "$RG" -n "$APP_NAME" \
    --query 'properties.configuration.ingress.ipSecurityRestrictions' -o json 2>/dev/null || true)"
  # az prints nothing (not "null") for an empty query result.
  print -r -- "${out:-null}"
}

try_keep() {
  local existing
  existing="$(live_rules)"
  VPN=("${(@f)$(print -r -- "$existing" | jq -r '(. // [])[] | select(.name | startswith("pu-vpn-")) | .ipAddressRange')}")
  (( ${#VPN} )) && [[ -n "${VPN[1]}" ]] || return 1
  SOURCE=keep SOURCE_DETAIL="${#VPN} pu-vpn-* rules already on $APP_NAME"
  gh_warning "ip-gate($SERVER): no live or static source; keeping the ${#VPN} VPN rules already applied"
}

# sane_ranges — every source is data: IPv4, strict CIDR, no wider than /8, capped count.
sane_ranges() {
  local out
  if ! out="$(print -rl -- "${VPN[@]}" | "$CHAT_PY" "$CHAT_ROOT/tools/probe.py" cidrs --min-prefix 8 --max-count 200 2>&1)"; then
    gh_warning "ip-gate($SERVER): $SOURCE ranges rejected: $out"
    return 1
  fi
  VPN=("${(@f)out}")
}

compute_rules() {
  { try_live && sane_ranges; } || { try_static && sane_ranges; } || { try_keep && sane_ranges; } \
    || die "ip-gate($SERVER): no valid VPN ranges from pugwips, the static link, or the live app; refusing to apply a campus-only allowlist" 3

  local -a rules
  local i=1 r
  add_rule() {  # name description cidr
    local cidr="$3"
    [[ "$cidr" == */* ]] || cidr="$cidr/32"
    [[ "$cidr" == *:* ]] && { log_warn "skipping IPv6 $cidr (Container Apps rules are IPv4 only)"; return 0; }
    rules+=("$(jq -nc --arg n "$1" --arg d "$2" --arg c "$cidr" '{name: $n, description: $d, ipAddressRange: $c, action: "Allow"}')")
  }
  for r in "${(@f)$(jqp '.ip_gate.campus_ranges[]')}"; do add_rule "pu-campus-$i" "Princeton campus" "$r"; i=$(( i + 1 )); done
  i=1
  for r in "${(@f)$(jq -r '.ip_gate.extra_ranges[]' "$PLATFORM_JSON")}"; do
    [[ -n "$r" ]] || continue
    add_rule "pu-extra-$i" "Extra allowed range" "$r"; i=$(( i + 1 ))
  done
  i=1
  for r in "${(@u)VPN}"; do add_rule "pu-vpn-$i" "PU VPN ($SOURCE, $PREFIX_MODE)" "$r"; i=$(( i + 1 )); done
  # The -mgmt job reaches the sidecars through this app's ingress from inside the environment.
  add_rule "aca-internal" "Container Apps environment subnet" "$(jqp .network.aca_subnet)"

  log_ok "ip-gate($SERVER): ${#rules} rules — VPN from $SOURCE ($SOURCE_DETAIL), mode $PREFIX_MODE"
  summary "- **ip-gate $SERVER**: ${#rules} rules; VPN ranges from **$SOURCE** ($SOURCE_DETAIL), mode \`$PREFIX_MODE\`"
  print -r -- "[${(j:,:)rules}]" | jq .
}

apply_rules() {
  local desired="$1" current name
  current="$(live_rules)"
  [[ -n "$current" && "$current" != null ]] || current='[]'
  # Upsert every desired rule, then remove whatever is no longer wanted (never the
  # temporary CI rule, which its own step removes).
  for row in "${(@f)$(print -r -- "$desired" | jq -c '.[]')}"; do
    [[ -n "$row" ]] || continue
    az_retry containerapp ingress access-restriction set -g "$RG" -n "$APP_NAME" \
      --rule-name "$(print -r -- "$row" | jq -r .name)" \
      --ip-address "$(print -r -- "$row" | jq -r .ipAddressRange)" \
      --description "$(print -r -- "$row" | jq -r .description)" \
      --action Allow --output none
  done
  for name in "${(@f)$(jq -rn --argjson c "$current" --argjson d "$desired" --arg t "$TEMP_RULE" \
      '[$c[].name] - [$d[].name] - [$t] | .[]')}"; do
    [[ -n "$name" ]] || continue
    az_retry containerapp ingress access-restriction remove -g "$RG" -n "$APP_NAME" --rule-name "$name" --output none
    log_info "removed stale rule $name"
  done
  log_ok "ip-gate($SERVER): applied to $APP_NAME"
}

case "$MODE" in
  emit)
    compute_rules
    ;;
  apply)
    az_login
    app_exists || die "$APP_NAME does not exist yet; deploy-server.zsh applies the gate on first deploy"
    # Assign first: errexit does not see a failed $(...) used as an argument.
    desired="$(compute_rules)"
    apply_rules "$desired"
    ;;
  add-temp)
    [[ "$TEMP_IP" =~ '^([0-9]{1,3}\.){3}[0-9]{1,3}$' ]] || die "--add-temp needs a bare IPv4 address"
    az_login
    az_retry containerapp ingress access-restriction set -g "$RG" -n "$APP_NAME" --rule-name "$TEMP_RULE" \
      --ip-address "$TEMP_IP/32" --description "Temporary: CI smoke test" --action Allow --output none
    log_ok "temporarily allowed $TEMP_IP"
    ;;
  remove-temp)
    az_login
    if live_rules | jq -e --arg t "$TEMP_RULE" '(. // []) | any(.name == $t)' >/dev/null; then
      az_retry containerapp ingress access-restriction remove -g "$RG" -n "$APP_NAME" --rule-name "$TEMP_RULE" --output none
      log_ok "removed temporary rule"
    else
      log_info "no temporary rule present"
    fi
    ;;
esac
