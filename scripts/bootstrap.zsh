#!/usr/bin/env zsh
# bootstrap.zsh — first deployment of a department, end to end, from an operator's Mac.
#
#   scripts/bootstrap.zsh --config <config-repo>/<dept> [--image <registry>/chat@sha256:...]
#                         [--server name ...] [--from STEP] [--only STEP]
#                         [--set-gh-vars] [--yes]
#
# Steps (each idempotent; rerun with --from to resume after fixing something):
#   prereqs   tools, Python venv, Azure login, chat.yml renders cleanly
#   platform  deploy-platform.zsh — RG, Key Vault, network, environment, PostgreSQL, storage,
#             container registry
#   image     build-image.zsh — Zulip image built in the registry from template.lock's
#             commit (this checkout must be that commit); sidecar images imported
#   github    setup-github-oidc.zsh — CI identity (+ GitHub Environment vars with --set-gh-vars)
#   secrets   Resend API key → Key Vault (or, with email.provider acs, acs-email.zsh:
#             SMTP credentials + domain verification); optional PUGWIPS_READ_TOKEN;
#             with healthchecks.enabled, the ping key (→ Key Vault + GitHub secret
#             HEALTHCHECKS_PING_KEY) and optional API key (→ Key Vault)
#   entra     entra-app.zsh per server — Zulip sign-in app registration, secret in Key Vault
#   access    grant-access.zsh per server — its two identities, each able to read only
#             its own Key Vault secrets
#   servers   deploy-server.zsh per server (groups first, the redirect host last)
#   healthchecks  healthchecks.zsh --sync (when healthchecks.enabled and an API key exists)
#   smoke     smoke.zsh per server
#   dns       the DNS request for OIT, written to dns-request-<dept>.md
#
# No DNS is needed for any of it. With dns: pending (the default) every server is
# configured for, and reachable at, https://<app>.<environment default domain>/ — sign-in
# included — so you can test while OIT works on the CNAMEs. Going live afterwards:
#   1. OIT creates the records in dns-request-<dept>.md;
#   2. scripts/bind-domain.zsh --config <dept> --server <name> [--wait 30]
#   3. set dns: live for that server in chat.yml, render, rerun entra-app.zsh, push.
source "${0:A:h}/common.zsh"
STEPS=(prereqs platform image github secrets entra access servers healthchecks smoke dns)
FROM="" ONLY="" IMAGE="${CHAT_IMAGE:-}" SET_GH_VARS=false
typeset -a ONLY_SERVERS
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --image) IMAGE="${2:?}"; shift 2 ;;
    --from) FROM="${2:?}"; shift 2 ;;
    --only) ONLY="${2:?}"; shift 2 ;;
    --server-only|--servers) ONLY_SERVERS+=("${2:?}"); shift 2 ;;
    --set-gh-vars) SET_GH_VARS=true; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$SERVER" ]] && ONLY_SERVERS+=("$SERVER")
for s in "$FROM" "$ONLY"; do
  [[ -z "$s" || ${STEPS[(Ie)$s]} -gt 0 ]] || die "unknown step '$s' (steps: ${STEPS[*]})"
done
YES=(); $ASSUME_YES && YES=(--yes)
S="${0:A:h}"

run_step() {  # run_step <name> — honours --from / --only
  local name="$1"
  if [[ -n "$ONLY" ]]; then [[ "$name" == "$ONLY" ]]; return; fi
  if [[ -n "$FROM" ]]; then (( ${STEPS[(Ie)$name]} >= ${STEPS[(Ie)$FROM]} )); return; fi
  return 0
}

# ------------------------------------------------------------------ prereqs
if run_step prereqs; then
  log_step "prereqs"
  require_cmd az jq openssl dig curl python3
  [[ -x "$CHAT_PY" ]] || "$S/setup-venv.zsh"
  "$CHAT_PY" "$CHAT_RENDER" render "$CONFIG_DIR" --check \
    || die "generated/ is stale or chat.yml is invalid; fix it, then: $CHAT_PY $CHAT_RENDER render $CONFIG_DIR"
fi
load_platform
DEPT="$(jqp .department)"
typeset -a SERVERS
SERVERS=("${(@f)$(server_names)}")
if (( ${#ONLY_SERVERS} )); then
  for s in "${ONLY_SERVERS[@]}"; do (( ${SERVERS[(Ie)$s]} )) || die "no server '$s' in chat.yml"; done
  SERVERS=("${ONLY_SERVERS[@]}")
fi
# Redirect host last, so its /<slug> redirects can point at servers that already exist.
REDIRECT_HOST="$(jq -r '.redirect_host // empty' "$CONFIG_DIR/generated/index.json")"
if [[ -n "$REDIRECT_HOST" && ${SERVERS[(Ie)$REDIRECT_HOST]} -gt 0 ]]; then
  SERVERS=("${(@)SERVERS:#$REDIRECT_HOST}" "$REDIRECT_HOST")
fi
az_login
log_info "department $DEPT, servers: ${SERVERS[*]}, image: ${IMAGE:-built from template.lock}"

# ------------------------------------------------------------------ platform
if run_step platform; then
  log_step "platform"
  "$S/deploy-platform.zsh" --config "$CONFIG_DIR" "${YES[@]}"
fi

# ------------------------------------------------------------------ image
if run_step image; then
  log_step "image"
  "$S/build-image.zsh" --config "$CONFIG_DIR" >/dev/null
fi

# ------------------------------------------------------------------ github
if run_step github; then
  log_step "github"
  gh_args=(); $SET_GH_VARS && gh_args+=(--set-gh-vars)
  "$S/setup-github-oidc.zsh" --config "$CONFIG_DIR" "${YES[@]}" "${gh_args[@]}"
fi

# ------------------------------------------------------------------ secrets
if run_step secrets; then
  log_step "secrets"
  if [[ "$(jqp .email.provider)" == acs ]]; then
    # ACS: an Entra app's client secret is the SMTP password; acs-email.zsh makes it.
    "$S/acs-email.zsh" --config "$CONFIG_DIR" "${YES[@]}"
  elif kv_secret_exists email-password; then
    log_ok "email-password (Resend API key) present"
  else
    [[ -t 0 ]] || die "Key Vault needs email-password (the Resend API key); run bootstrap interactively or: az keyvault secret set --vault-name $KV_NAME --name email-password --file <file>"
    print -u2 -r -- "Zulip sends mail through Resend SMTP as $(jqp .email.from)."
    print -u2 -r -- "Create an API key with sending access for that domain at https://resend.com/api-keys"
    read -rs "key?Resend API key (input hidden): "; print -u2
    [[ "$key" == re_* ]] || log_warn "that does not look like a Resend key (re_...); storing it anyway"
    print -rn -- "$key" | kv_secret_set_from_stdin email-password
    unset key
    log_ok "stored email-password"
  fi
  if hc_enabled; then
    gh_ok=false
    if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then gh_ok=true; fi
    if kv_secret_exists healthchecks-ping-key; then
      log_ok "healthchecks-ping-key present"
    else
      [[ -t 0 ]] || die "healthchecks.enabled needs Key Vault healthchecks-ping-key; run bootstrap interactively"
      print -u2 -r -- "Healthchecks: project Settings → Ping key (pings go to $(jqp .healthchecks.ping_base)/<key>/<slug>)."
      read -rs "hkey?Healthchecks ping key (input hidden): "; print -u2
      print -rn -- "$hkey" | kv_secret_set_from_stdin healthchecks-ping-key
      if $gh_ok && confirm "Also store it as GitHub secret HEALTHCHECKS_PING_KEY on $(jqp .github.repo) (CI pings)?"; then
        print -rn -- "$hkey" | gh secret set HEALTHCHECKS_PING_KEY --repo "$(jqp .github.repo)"
        log_ok "HEALTHCHECKS_PING_KEY set for CI"
      else
        gh_warning "set the GitHub secret HEALTHCHECKS_PING_KEY yourself, or CI keepalive/update pings are skipped"
      fi
      unset hkey
      log_ok "stored healthchecks-ping-key"
    fi
    if ! kv_secret_exists healthchecks-api-key && [[ -t 0 ]] \
       && confirm "Store a Healthchecks API key so checks get the right schedules (recommended)?"; then
      read -rs "akey?Healthchecks API key, read-write (input hidden): "; print -u2
      print -rn -- "$akey" | kv_secret_set_from_stdin healthchecks-api-key
      unset akey
      log_ok "stored healthchecks-api-key"
    fi
  fi
  gated=($(for s in "${SERVERS[@]}"; do jq -r 'select(.ip_gate) | .name' "$CONFIG_DIR/generated/servers/$s.json"; done))
  if (( ${#gated} )); then
    if command -v gh >/dev/null && gh auth status >/dev/null 2>&1 && [[ -t 0 ]] \
       && confirm "Set PUGWIPS_READ_TOKEN on $(jqp .github.repo) now? (Without it the IP gate uses the static snapshot)"; then
      read -rs "tok?pugwips read token (input hidden): "; print -u2
      # Repository-level: jobs that call the reusable workflows (and the snapshot
      # refresh) cannot see environment secrets.
      print -rn -- "$tok" | gh secret set PUGWIPS_READ_TOKEN --repo "$(jqp .github.repo)"
      unset tok
      log_ok "PUGWIPS_READ_TOKEN set for CI"
    else
      gh_warning "no PUGWIPS_READ_TOKEN: gated servers (${gated[*]}) use the static snapshot/fallback link"
    fi
  fi
fi

# ------------------------------------------------------------------ entra
if run_step entra; then
  for s in "${SERVERS[@]}"; do
    log_step "entra: $s"
    "$S/entra-app.zsh" --config "$CONFIG_DIR" --server "$s" "${YES[@]}"
  done
fi

# ------------------------------------------------------------------ access
if run_step access; then
  for s in "${SERVERS[@]}"; do
    log_step "access: $s"
    "$S/grant-access.zsh" --config "$CONFIG_DIR" --server "$s"
  done
fi

# ------------------------------------------------------------------ servers
if run_step servers; then
  img_args=(); [[ -n "$IMAGE" ]] && img_args=(--image "$IMAGE")
  for s in "${SERVERS[@]}"; do
    log_step "server: $s"
    "$S/deploy-server.zsh" --config "$CONFIG_DIR" --server "$s" "${img_args[@]}" "${YES[@]}"
  done
  # Redirects are resolved at deploy time; if the redirect host went first on an earlier
  # run, its /<slug> targets may have been skipped. Redeploying it is cheap and idempotent.
fi

# ------------------------------------------------------------------ healthchecks
if run_step healthchecks; then
  if hc_enabled; then
    log_step "healthchecks"
    if kv_secret_exists healthchecks-api-key; then
      "$S/healthchecks.zsh" --config "$CONFIG_DIR" --sync
    else
      log_warn "no healthchecks-api-key: checks appear on first ping with default schedules; set timeouts in the Healthchecks UI"
    fi
  fi
fi

# ------------------------------------------------------------------ smoke
if run_step smoke; then
  for s in "${SERVERS[@]}"; do
    log_step "smoke: $s"
    ip_args=(); [[ "$(jq -r .ip_gate "$CONFIG_DIR/generated/servers/$s.json")" == true ]] && ip_args=(--runner-ip auto)
    "$S/smoke.zsh" --config "$CONFIG_DIR" --server "$s" "${ip_args[@]}"
  done
fi

# ------------------------------------------------------------------ dns
if run_step dns; then
  log_step "dns request for OIT"
  OUT_FILE="${CHAT_OUT_DIR:-$PWD}/dns-request-$DEPT.md"
  {
    print -r -- "# DNS request — $DEPT chat"
    print -r -- ""
    for s in "${SERVERS[@]}"; do
      "$S/bind-domain.zsh" --config "$CONFIG_DIR" --server "$s" --print
      print -r -- ""
    done
    if [[ "$(jqp .email.provider)" == acs && "$(jqp .acs.managed)" != true ]]; then
      "$S/acs-email.zsh" --config "$CONFIG_DIR" --print
    fi
  } > "$OUT_FILE"
  log_ok "wrote $OUT_FILE — send it to OIT (hostmaster) to create the records"
fi

log_step "Reachable now (before DNS)"
DOMAIN="$(env_default_domain 2>/dev/null || true)"
[[ -n "$DOMAIN" ]] && "$CHAT_PY" "$CHAT_RENDER" urls "$CONFIG_DIR" --default-domain "$DOMAIN" >&2
cat >&2 <<EOT

Next:
  * Sign in at the "now" URLs above with your Princeton account and check chat works.
    Mobile/desktop apps can be pointed at those URLs too (re-add them after going live).
  * Commit and push the config repo; CI now deploys every change to chat.yml.
  * When OIT has created the records in dns-request-$DEPT.md, for each server:
      $S/bind-domain.zsh --config $CONFIG_DIR --server <name> --wait 30
    then set dns: live for it in chat.yml, render, rerun entra-app.zsh, commit and push.
EOT
