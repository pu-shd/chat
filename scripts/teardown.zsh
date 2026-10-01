#!/usr/bin/env zsh
# teardown.zsh — reverse of bootstrap.zsh for a department (or some of its servers).
#
#   scripts/teardown.zsh --config <dept> [--server <name> ...] [--purge]
#                        [--platform] [--entra] [--github] [--healthchecks]
#
# Always (for the selected servers, all by default):
#   servers       teardown-server.zsh — app and its -mgmt/-hc jobs deleted. Database,
#                 uploads and secrets kept unless --purge (then destroyed).
# Only when asked:
#   --healthchecks  delete the servers' Healthchecks checks (needs the API key)
#   --entra         delete the servers' Zulip sign-in app registrations (operator; CI
#                   has no Entra rights)
#   --platform      delete the whole resource group (needs --purge and every server)
#   --github        delete the CI app registration and the GitHub Environment/variables
#
# --server may be repeated (--also-server <name> is the same); every one is torn down.
#
# One confirmation covers the run, and it names what it covers: type
# "TEARDOWN <dept> ALL" (no --server), or "TEARDOWN <dept> <server> [<server> ...]" (the
# selected servers, sorted) — each with " PURGE" appended for --purge. So the phrase for
# one server can never confirm a teardown of all of them. CHAT_CONFIRM supplies it
# non-interactively (the Teardown workflow).
source "${0:A:h}/common.zsh"
PURGE=false PLATFORM=false ENTRA=false GITHUB=false HC=false
typeset -a ONLY ARGS
# parse_common_args keeps only the last --server; collect every one first.
while (( $# )); do
  case "$1" in
    --server) ONLY+=("${2:?--server needs a name}"); shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
parse_common_args "${ARGS[@]}"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --purge) PURGE=true; shift ;;
    --platform) PLATFORM=true; shift ;;
    --entra) ENTRA=true; shift ;;
    --github) GITHUB=true; shift ;;
    --healthchecks) HC=true; shift ;;
    --also-server) ONLY+=("${2:?}"); shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
load_platform
DEPT="$(jqp .department)"
S="${0:A:h}"

typeset -a ALL SERVERS
ALL=("${(@f)$(server_names)}")
if (( ${#ONLY} )); then
  for s in "${ONLY[@]}"; do (( ${ALL[(Ie)$s]} )) || die "no server '$s' in chat.yml"; done
  SERVERS=("${(@u)ONLY}")
else
  SERVERS=("${ALL[@]}")
fi
if $PLATFORM; then
  $PURGE || die "--platform deletes every database and upload: it needs --purge too"
  (( ${#SERVERS} == ${#ALL} )) || die "--platform needs every server (drop --server)"
fi
if $GITHUB && ! $PLATFORM; then
  die "--github removes CI's access; only together with --platform"
fi

# Checks can only be deleted with the API key; without it, say so and carry on.
hc_manageable() {
  hc_enabled || return 1
  if [[ -n "${HEALTHCHECKS_API_KEY:-}" ]] || kv_secret_exists healthchecks-api-key; then return 0; fi
  gh_warning "no Healthchecks API key: delete the checks for ${SERVERS[*]} in the Healthchecks UI"
  return 1
}

if (( ${#ONLY} )); then phrase="TEARDOWN $DEPT ${(j: :)${(@o)SERVERS}}"; else phrase="TEARDOWN $DEPT ALL"; fi
$PURGE && phrase+=" PURGE"
log_warn "about to remove: servers ${SERVERS[*]} ($($PURGE && print "PURGE: databases, uploads and secrets destroyed" || print "data kept"))"
$HC && log_warn "  + their Healthchecks checks"
$ENTRA && log_warn "  + their Entra sign-in app registrations"
$PLATFORM && log_warn "  + the whole resource group $RG"
$GITHUB && log_warn "  + CI's app registration and GitHub Environment $(jqp .github.environment)"
confirm_typed "$phrase"
az_login

for srv in "${SERVERS[@]}"; do
  log_step "server $srv"
  if $PURGE; then
    CHAT_CONFIRM="PURGE $srv" "$S/teardown-server.zsh" --config "$CONFIG_DIR" --server "$srv" --purge
  else
    CHAT_CONFIRM="CONFIRM-DELETE $srv" "$S/teardown-server.zsh" --config "$CONFIG_DIR" --server "$srv"
  fi
  if $HC && hc_manageable; then
    CHAT_CONFIRM="DELETE-CHECKS $srv" "$S/healthchecks.zsh" --config "$CONFIG_DIR" --server "$srv" --delete
  fi
  if $ENTRA; then
    # Only an app we own: never delete another team's look-alike.
    app="$(entra_app_by_name "$PREFIX-$srv-zulip")" || die "not deleting app registration $PREFIX-$srv-zulip"
    if [[ -n "$app" ]]; then
      az ad app delete --id "$app"
      log_ok "deleted Entra app $PREFIX-$srv-zulip ($app)"
    fi
  fi
done

if $PLATFORM; then
  log_step "platform $RG"
  CHAT_CONFIRM="DELETE-PLATFORM $RG" "$S/teardown-platform.zsh" --config "$CONFIG_DIR"
  if $HC && hc_manageable; then
    # Department-wide checks (e.g. <prefix>-updates) outlive the servers.
    CHAT_CONFIRM="DELETE-CHECKS $DEPT" "$S/healthchecks.zsh" --config "$CONFIG_DIR" --delete || true
  fi
fi

if $GITHUB; then
  log_step "GitHub"
  app="$(entra_app_by_name "$(jqp .names.github_app)")" || die "not deleting app registration $(jqp .names.github_app)"
  if [[ -n "$app" ]]; then az ad app delete --id "$app"; log_ok "deleted CI app registration ($app)"; fi
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    repo="$(jqp .github.repo)"
    gh api -X DELETE "repos/$repo/environments/$(jqp .github.environment)" --silent || true
    for v in AZURE_CLIENT_ID AZURE_TENANT_ID AZURE_SUBSCRIPTION_ID; do gh variable delete "$v" --repo "$repo" 2>/dev/null || true; done
    log_ok "removed GitHub Environment and variables from $repo"
  else
    log_warn "gh not authenticated: remove the GitHub Environment and AZURE_* variables by hand"
  fi
fi

log_ok "teardown of ${SERVERS[*]} complete"
if ! $PLATFORM; then
  log_info "the platform ($RG) is still there; redeploy with bootstrap.zsh --from servers, or remove it with --purge --platform"
fi
summary "- teardown $DEPT: ${SERVERS[*]} ($($PURGE && print purge || print preserve))$($PLATFORM && print ', platform deleted')"
