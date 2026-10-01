#!/usr/bin/env zsh
# keepalive.zsh — daily "is everything still fine" for a department, from outside Azure.
#
#   scripts/keepalive.zsh --config <dept> [--server <name>] [--runner-ip auto|<ip>]
#
# Per server:
#   * smoke.zsh: every reachable realm answers, Entra sign-in redirects, /<slug> redirects;
#   * TLS certificate on each live hostname: warn under 30 days, fail under 14 (managed
#     certificates renew themselves; a Key Vault certificate is renewed by a person);
#   * the Entra client secret's expiry (recorded on Key Vault <server>-oidc-secret by
#     entra-app.zsh): warn under 60 days, fail under 21;
#   * with email.provider acs, the SMTP client secret's expiry (same thresholds);
#   * the IP gate's static snapshot age (gated servers): warn past snapshot_max_age_days.
# Then pings <prefix>-<server>-web on Healthchecks (success, or fail with the reasons).
# Exits non-zero if any server failed, so the workflow run is red too.
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
az_login
PROBE=("$CHAT_PY" "$CHAT_ROOT/tools/probe.py")
typeset -a SERVERS
if [[ -n "$SERVER" ]]; then SERVERS=("$SERVER"); else SERVERS=("${(@f)$(server_names)}"); fi

failed_servers=()
for srv in "${SERVERS[@]}"; do
  load_server "$srv"
  log_step "keepalive: $srv"
  typeset -a problems warnings_
  problems=() warnings_=()

  smoke_args=(--config "$CONFIG_DIR" --server "$srv")
  if [[ "$(jqs .ip_gate)" == true && -n "$RUNNER_IP" ]]; then smoke_args+=(--runner-ip "$RUNNER_IP"); fi
  if ! "${0:A:h}/smoke.zsh" "${smoke_args[@]}"; then
    problems+=("smoke test failed")
  fi

  if [[ "$(jqs .dns)" == live ]]; then
    for h in "${(@f)$(jqs '.hosts[]')}"; do
      if days="$("${PROBE[@]}" cert-days "$h" 2>/dev/null)"; then
        if (( days < 14 )); then problems+=("TLS certificate for $h expires in $days days")
        elif (( days < 30 )); then warnings_+=("TLS certificate for $h expires in $days days"); fi
      else
        problems+=("could not read the TLS certificate of $h")
      fi
    done
  fi

  expires="$(az keyvault secret show --vault-name "$KV_NAME" --name "$srv-oidc-secret" --query attributes.expires -o tsv 2>/dev/null || true)"
  if [[ -z "$expires" ]]; then
    warnings_+=("$srv-oidc-secret has no expiry recorded; rerun entra-app.zsh --rotate-secret to record it")
  elif days="$("${PROBE[@]}" days-until "$expires" 2>/dev/null)"; then
    if (( days < 21 )); then problems+=("Entra client secret expires in $days days: entra-app.zsh --server $srv --rotate-secret, then update-server.zsh --restart")
    elif (( days < 60 )); then warnings_+=("Entra client secret expires in $days days"); fi
  else
    problems+=("could not read the expiry '$expires' of $srv-oidc-secret")
  fi

  if [[ "$(jqp .email.provider)" == acs ]]; then
    # ACS SMTP authenticates with an Entra client secret: it expires like the OIDC one.
    mexp="$(az keyvault secret show --vault-name "$KV_NAME" --name email-password --query attributes.expires -o tsv 2>/dev/null || true)"
    if [[ -z "$mexp" ]]; then
      warnings_+=("email-password has no expiry recorded; rerun acs-email.zsh --rotate-secret to record it")
    elif days="$("${PROBE[@]}" days-until "$mexp" 2>/dev/null)"; then
      if (( days < 21 )); then problems+=("ACS SMTP secret expires in $days days: acs-email.zsh --rotate-secret, then restart the servers")
      elif (( days < 60 )); then warnings_+=("ACS SMTP secret expires in $days days"); fi
    else
      problems+=("could not read the expiry '$mexp' of email-password")
    fi
  fi

  if [[ "$(jqs .ip_gate)" == true ]]; then
    snap="$CONFIG_DIR/$(jqp .ip_gate.snapshot)"
    if [[ -f "$snap" ]]; then
      # Same fields as ip-gate.zsh: snapshot_date, else resolved_at. An unreadable date is
      # a problem for this server, never a crash of the whole run.
      if age="$("$CHAT_PY" -c 'import sys, json, datetime as d
s = json.load(open(sys.argv[1]))
v = (s.get("snapshot_date") or s.get("resolved_at")) if isinstance(s, dict) else None
print((d.date.today() - d.date.fromisoformat(str(v)[:10])).days) if v else sys.exit(2)' "$snap" 2>/dev/null)"; then
        if (( age > $(jqp .ip_gate.snapshot_max_age_days) )); then warnings_+=("pugwips snapshot is $age days old"); fi
      else
        problems+=("pugwips snapshot $snap has no readable snapshot_date or resolved_at")
      fi
    fi
  fi

  for w in "${warnings_[@]}"; do gh_warning "keepalive($srv): $w"; done
  if (( ${#problems} )); then
    for p in "${problems[@]}"; do log_error "keepalive($srv): $p"; done
    hc_ping "$(jqs .healthchecks.web)" fail "${(j:; :)problems}"
    summary "- ❌ **$srv**: ${(j:; :)problems}"
    failed_servers+=("$srv")
  else
    hc_ping "$(jqs .healthchecks.web)" success "ok${warnings_:+ (warnings: ${(j:; :)warnings_})}"
    summary "- ✅ **$srv**${warnings_:+ — ${(j:; :)warnings_}}"
    log_ok "keepalive($srv): ok"
  fi
done

(( ${#failed_servers} == 0 )) || die "keepalive failed for: ${failed_servers[*]}"
log_ok "keepalive: all ${#SERVERS} server(s) fine"
