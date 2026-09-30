#!/usr/bin/env zsh
# bootstrap.zsh — first deployment of a department, end to end, from an operator's Mac.
#
#   scripts/bootstrap.zsh --config <config-repo>/<dept>
#       [--resume | --restart] [--from STEP] [--only STEP] [--step-by-step]
#       [--server NAME ...] [--image <registry>/chat@sha256:...] [--set-gh-vars] [--yes]
#
# Every step is idempotent, so running it again is always safe. Each run records what
# finished in <config repo>/.chat-bootstrap/<dept>.state (gitignored), so it can resume:
#   * on a terminal, a rerun shows the checklist and offers to resume at the first step
#     that is not done (or start over, or pick a step);
#   * --resume skips steps already done (a non-interactive rerun otherwise runs them all
#     again), --restart forgets the record, --from / --only pick steps;
#   * --step-by-step asks before each step; after a failure an interactive run offers
#     retry / skip / quit, and a non-interactive one stops and prints how to resume.
# A step done with a different template commit than this checkout counts as stale and
# runs again.
#
# Steps:
#   prereqs      tools, Python venv, Azure login, chat.yml renders cleanly, sanity checks
#   platform     deploy-platform.zsh — RG, Key Vault, network, environment, PostgreSQL,
#                storage, container registry
#   image        build-image.zsh — Zulip image built in the registry from template.lock's
#                commit (this checkout must be that commit); sidecar images imported
#   github       setup-github-oidc.zsh — CI identity (+ repo protection, environments and
#                variables with --set-gh-vars)
#   secrets      mail credentials (Resend key or ACS), Healthchecks keys, PUGWIPS_READ_TOKEN
#   entra        entra-app.zsh per server — Zulip sign-in app registration
#   access       grant-access.zsh per server — identities, per-secret Key Vault access, AcrPull
#   servers      deploy-server.zsh per server (groups first, the redirect host last)
#   healthchecks healthchecks.zsh --sync (when enabled and an API key exists)
#   smoke        smoke.zsh per server
#   dns          the DNS request for OIT, written to dns-request-<dept>.md
#
# No DNS is needed for any of it: with dns: pending (the default) every server answers on
# https://<app>.<environment default domain>/, sign-in included.
source "${0:A:h}/common.zsh"

typeset -a STEPS
STEPS=(prereqs platform image github secrets entra access servers healthchecks smoke dns)
typeset -A DESC
DESC=(
  prereqs      "tools, venv, Azure login, chat.yml renders cleanly"
  platform     "resource group, Key Vault, network, environment, PostgreSQL, storage, registry"
  image        "build the Zulip image in the registry; import the sidecar images"
  github       "CI's Azure identity (and GitHub protections/variables with --set-gh-vars)"
  secrets      "mail credentials, Healthchecks keys, PUGWIPS_READ_TOKEN"
  entra        "Zulip's Entra sign-in app registration, per server"
  access       "per-server identities: their Key Vault secrets and AcrPull"
  servers      "deploy every server (the redirect host last)"
  healthchecks "create/tune the Healthchecks checks"
  smoke        "check every reachable realm, Entra sign-in and the redirects"
  dns          "write the DNS request for OIT"
)

FROM="" ONLY="" IMAGE="${CHAT_IMAGE:-}" SET_GH_VARS=false STEPWISE=false MODE="" INTERNAL_STEP=""
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
    --step-by-step|-i) STEPWISE=true; shift ;;
    --resume) MODE=resume; shift ;;
    --restart) MODE=restart; shift ;;
    --_step) INTERNAL_STEP="${2:?}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$SERVER" ]] && ONLY_SERVERS+=("$SERVER")
for s in "$FROM" "$ONLY"; do
  [[ -z "$s" || ${STEPS[(Ie)$s]} -gt 0 ]] || die "unknown step '$s' (steps: ${STEPS[*]})"
done
YES=(); $ASSUME_YES && YES=(--yes)
S="${0:A:h}"
INTERACTIVE=false
if [[ -t 0 && -t 2 ]] && ! $ASSUME_YES; then INTERACTIVE=true; fi

# ------------------------------------------------------------------ look and feel
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  _b=$'\e[1m' _d=$'\e[2m' _g=$'\e[32m' _r=$'\e[31m' _y=$'\e[33m' _c=$'\e[36m' _m=$'\e[35m' _o=$'\e[0m'
else
  _b='' _d='' _g='' _r='' _y='' _c='' _m='' _o=''
fi
line() { print -u2 -r -- "${_d}────────────────────────────────────────────────────────────────${_o}"; }
banner() {
  line
  print -u2 -r -- "${_b}${_m}  pu-shd/chat bootstrap${_o}  ${_b}$1${_o}"
  shift
  local l
  for l in "$@"; do print -u2 -r -- "  ${_d}$l${_o}"; done
  line
}
fmt_duration() { if (( $1 >= 60 )); then print -rn -- "$(( $1 / 60 ))m$(( $1 % 60 ))s"; else print -rn -- "${1}s"; fi; }

# ------------------------------------------------------------------ state
STATE_DIR="${CHAT_STATE_DIR:-${CONFIG_DIR:h}/.chat-bootstrap}"
STATE_FILE="$STATE_DIR/${CONFIG_DIR:t}.state"
TEMPLATE_SHA="$(git -C "$CHAT_ROOT" rev-parse HEAD 2>/dev/null || print unknown)"

state_get() {  # state_get <step> <field>
  [[ -f "$STATE_FILE" ]] || return 0
  jq -r --arg s "$1" --arg f "$2" '.[$s][$f] // empty' "$STATE_FILE" 2>/dev/null || true
}
state_set() {  # state_set <step> <status> <seconds>
  mkdir -p "$STATE_DIR"
  local cur tmp
  cur="$(cat "$STATE_FILE" 2>/dev/null || print '{}')"
  tmp="$(mktemp)"
  print -r -- "$cur" | jq --arg s "$1" --arg st "$2" --argjson sec "${3:-0}" --arg sha "$TEMPLATE_SHA" \
    --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '.[$s] = {status: $st, seconds: $sec, template: $sha, at: $at}' > "$tmp"
  mv "$tmp" "$STATE_FILE"
}
step_status() {  # done | failed | stale | pending
  local st sha
  st="$(state_get "$1" status)"
  sha="$(state_get "$1" template)"
  if [[ "$st" == done && -n "$sha" && "$sha" != "$TEMPLATE_SHA" ]]; then print stale; return 0; fi
  print -r -- "${st:-pending}"
}
checklist() {
  local s st mark when
  for s in "${STEPS[@]}"; do
    st="$(step_status "$s")"
    case "$st" in
      done)   mark="${_g}✓${_o}" ;;
      failed) mark="${_r}✗${_o}" ;;
      stale)  mark="${_y}↻${_o}" ;;
      *)      mark="${_d}·${_o}" ;;
    esac
    when=""
    if [[ "$st" != pending ]]; then when="  ${_d}($st $(state_get "$s" at))${_o}"; fi
    print -u2 -r -- "  $mark ${_b}$(printf '%-12s' "$s")${_o} ${_d}${DESC[$s]}${_o}$when"
  done
}
first_unfinished() {
  local s
  for s in "${STEPS[@]}"; do
    if [[ "$(step_status "$s")" != done ]]; then print -r -- "$s"; return 0; fi
  done
}

# ------------------------------------------------------------------ steps
# Each runs in its own process (bootstrap.zsh --_step NAME), so errexit behaves and a
# failure ends only that step.

step_prereqs() {
  require_cmd az jq openssl dig curl python3 git
  [[ -x "$CHAT_PY" ]] || "$S/setup-venv.zsh"
  "$CHAT_PY" "$CHAT_RENDER" render "$CONFIG_DIR" --check \
    || die "generated/ is stale or chat.yml is invalid; fix it, then: $CHAT_PY $CHAT_RENDER render $CONFIG_DIR"
  load_platform
  az_login
  log_ok "signed in as $(az account show --query user.name -o tsv) — subscription $(az account show --query name -o tsv)"
  local lock="$CONFIG_DIR/../template.lock" want
  want="$(jq -r '.sha // empty' "$lock" 2>/dev/null || true)"
  if [[ -n "$want" && "$want" != "$TEMPLATE_SHA" ]]; then
    gh_warning "this template checkout is ${TEMPLATE_SHA[1,7]} but template.lock pins ${want[1,7]}: git -C $CHAT_ROOT checkout $want"
  fi
  # Placeholders people forget.
  local admin s o
  admin="$(jqp .admin_email)"
  for s in "${(@f)$(server_names)}"; do
    for o in "${(@f)$(jq -r '.realms[] | select(.active) | .owner.email' "$CONFIG_DIR/generated/servers/$s.json")}"; do
      if [[ "$o" == "$admin" ]]; then
        log_warn "server $s: a realm owner is admin_email ($o) — a placeholder? The owner is the first person with full rights in that realm"
      fi
    done
  done
}
step_platform() { "$S/deploy-platform.zsh" --config "$CONFIG_DIR" "${YES[@]}"; }
step_image() { "$S/build-image.zsh" --config "$CONFIG_DIR" >/dev/null; }
step_github() {
  local gh_args=()
  if $SET_GH_VARS; then gh_args+=(--set-gh-vars); fi
  "$S/setup-github-oidc.zsh" --config "$CONFIG_DIR" "${YES[@]}" "${gh_args[@]}"
}
step_secrets() {
  az_login
  if [[ "$(jqp .email.provider)" == acs ]]; then
    # ACS: an Entra app's client secret is the SMTP password; acs-email.zsh makes it.
    "$S/acs-email.zsh" --config "$CONFIG_DIR" "${YES[@]}"
  elif kv_secret_exists email-password; then
    log_ok "email-password (mail API key) present"
  else
    [[ -t 0 ]] || die "Key Vault needs email-password (the mail API key); run bootstrap interactively or: az keyvault secret set --vault-name $KV_NAME --name email-password --file <file>"
    print -u2 -r -- "Zulip sends mail through $(jqp .email.host) as $(jqp .email.from)."
    if [[ "$(jqp .email.provider)" == resend ]]; then
      print -u2 -r -- "Create an API key with sending access for that domain at https://resend.com/api-keys"
    fi
    local key
    read -rs "key?${_c}Mail password / API key (input hidden):${_o} "; print -u2
    [[ -n "$key" ]] || die "nothing entered"
    if [[ "$(jqp .email.provider)" == resend && "$key" != re_* ]]; then
      log_warn "that does not look like a Resend key (re_...); storing it anyway"
    fi
    print -rn -- "$key" | kv_secret_set_from_stdin email-password
    unset key
    log_ok "stored email-password"
  fi
  if hc_enabled; then
    local gh_ok=false hkey akey
    if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then gh_ok=true; fi
    if kv_secret_exists healthchecks-ping-key; then
      log_ok "healthchecks-ping-key present"
    else
      [[ -t 0 ]] || die "healthchecks.enabled needs Key Vault healthchecks-ping-key; run bootstrap interactively"
      print -u2 -r -- "Healthchecks: project Settings → Ping key (pings go to $(jqp .healthchecks.ping_base)/<key>/<slug>)."
      read -rs "hkey?${_c}Healthchecks ping key (input hidden):${_o} "; print -u2
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
      read -rs "akey?${_c}Healthchecks API key, read-write (input hidden):${_o} "; print -u2
      print -rn -- "$akey" | kv_secret_set_from_stdin healthchecks-api-key
      unset akey
      log_ok "stored healthchecks-api-key"
    fi
  fi
  local -a gated
  gated=($(for s in "${SERVERS[@]}"; do jq -r 'select(.ip_gate) | .name' "$CONFIG_DIR/generated/servers/$s.json"; done))
  if (( ${#gated} )); then
    local tok
    if command -v gh >/dev/null && gh auth status >/dev/null 2>&1 && [[ -t 0 ]] \
       && confirm "Set PUGWIPS_READ_TOKEN on $(jqp .github.repo) now? (Without it the IP gate uses the static snapshot)"; then
      read -rs "tok?${_c}pugwips read token (input hidden):${_o} "; print -u2
      # Repository-level: jobs that call the reusable workflows cannot see environment secrets.
      print -rn -- "$tok" | gh secret set PUGWIPS_READ_TOKEN --repo "$(jqp .github.repo)"
      unset tok
      log_ok "PUGWIPS_READ_TOKEN set for CI"
    else
      gh_warning "no PUGWIPS_READ_TOKEN: gated servers (${gated[*]}) use the static snapshot/fallback link"
    fi
  fi
}
step_entra() {
  local s
  for s in "${SERVERS[@]}"; do
    log_info "${_b}entra: $s${_o}"
    "$S/entra-app.zsh" --config "$CONFIG_DIR" --server "$s" "${YES[@]}"
  done
}
step_access() {
  local s
  for s in "${SERVERS[@]}"; do
    log_info "${_b}access: $s${_o}"
    "$S/grant-access.zsh" --config "$CONFIG_DIR" --server "$s"
  done
}
step_servers() {
  local s img_args=()
  if [[ -n "$IMAGE" ]]; then img_args=(--image "$IMAGE"); fi
  for s in "${SERVERS[@]}"; do
    log_info "${_b}server: $s${_o}"
    "$S/deploy-server.zsh" --config "$CONFIG_DIR" --server "$s" "${img_args[@]}" "${YES[@]}"
  done
}
step_healthchecks() {
  if ! hc_enabled; then log_info "healthchecks.enabled is false; nothing to do"; return 0; fi
  az_login
  if kv_secret_exists healthchecks-api-key; then
    "$S/healthchecks.zsh" --config "$CONFIG_DIR" --sync
  else
    log_warn "no healthchecks-api-key: checks appear on first ping with default schedules; set timeouts in the Healthchecks UI"
  fi
}
step_smoke() {
  local s ip_args
  for s in "${SERVERS[@]}"; do
    log_info "${_b}smoke: $s${_o}"
    ip_args=()
    if [[ "$(jq -r .ip_gate "$CONFIG_DIR/generated/servers/$s.json")" == true ]]; then ip_args=(--runner-ip auto); fi
    "$S/smoke.zsh" --config "$CONFIG_DIR" --server "$s" "${ip_args[@]}"
  done
}
step_dns() {
  local out_file="${CHAT_OUT_DIR:-$PWD}/dns-request-$(jqp .department).md" s
  {
    print -r -- "# DNS request — $(jqp .department) chat"
    print -r -- ""
    for s in "${SERVERS[@]}"; do
      "$S/bind-domain.zsh" --config "$CONFIG_DIR" --server "$s" --print
      print -r -- ""
    done
    if [[ "$(jqp .email.provider)" == acs && "$(jqp .acs.managed)" != true ]]; then
      "$S/acs-email.zsh" --config "$CONFIG_DIR" --print
    fi
  } > "$out_file"
  log_ok "wrote $out_file — send it to OIT (hostmaster) to create the records"
}

# ------------------------------------------------------------------ servers in scope
typeset -ga SERVERS
select_servers() {
  SERVERS=("${(@f)$(server_names)}")
  if (( ${#ONLY_SERVERS} )); then
    local s
    for s in "${ONLY_SERVERS[@]}"; do (( ${SERVERS[(Ie)$s]} )) || die "no server '$s' in chat.yml"; done
    SERVERS=("${ONLY_SERVERS[@]}")
  fi
  # Redirect host last, so its /<slug> redirects can point at servers that already exist.
  local host
  host="$(jq -r '.redirect_host // empty' "$CONFIG_DIR/generated/index.json")"
  if [[ -n "$host" && ${SERVERS[(Ie)$host]} -gt 0 ]]; then
    SERVERS=("${(@)SERVERS:#$host}" "$host")
  fi
}

# Internal: run exactly one step in this (child) process.
if [[ -n "$INTERNAL_STEP" ]]; then
  if [[ "$INTERNAL_STEP" != prereqs ]]; then
    load_platform
    select_servers
  fi
  "step_$INTERNAL_STEP"
  exit 0
fi

# ------------------------------------------------------------------ plan the run
if [[ "$MODE" == restart ]]; then rm -f "$STATE_FILE"; fi
DEPT="$(jq -r .department "$CONFIG_DIR/generated/platform.json" 2>/dev/null || print "${CONFIG_DIR:t}")"
banner "$DEPT" "config    $CONFIG_DIR" "template  ${TEMPLATE_SHA[1,12]}  ($CHAT_ROOT)" "state     $STATE_FILE" \
  "$($INTERACTIVE && print interactive || print non-interactive)$($STEPWISE && print ', step by step')"

typeset -a RUN
if [[ -n "$ONLY" ]]; then
  RUN=("$ONLY")
elif [[ -n "$FROM" ]]; then
  RUN=("${(@)STEPS[${STEPS[(Ie)$FROM]},-1]}")
else
  start="${STEPS[1]}"
  if [[ -f "$STATE_FILE" ]]; then
    print -u2 -r -- "${_b}Progress so far${_o}"
    checklist
    next="$(first_unfinished)"
    if [[ -z "$next" ]]; then
      log_ok "every step is done for this template commit"
      if [[ "$MODE" == resume ]]; then exit 0; fi
      if $INTERACTIVE && ! confirm "Run everything again (it is idempotent)?"; then exit 0; fi
    elif [[ "$MODE" == resume ]]; then
      start="$next"
    elif $INTERACTIVE; then
      print -u2 -r -- ""
      read -r "ans?${_c}[r]esume at ${_b}$next${_o}${_c}, [s]tart over, [c]hoose a step, [q]uit? [r]${_o} "
      case "${ans:-r}" in
        [rR]*) start="$next" ;;
        [sS]*) rm -f "$STATE_FILE"; start="${STEPS[1]}" ;;
        [cC]*)
          read -r "pick?${_c}step (${STEPS[*]}):${_o} "
          (( ${STEPS[(Ie)$pick]} )) || die "unknown step '$pick'"
          start="$pick" ;;
        *) exit 0 ;;
      esac
    fi
  fi
  RUN=("${(@)STEPS[${STEPS[(Ie)$start]},-1]}")
fi

# The same options for every step's child process.
typeset -a CHILD
CHILD=(--config "$CONFIG_DIR" "${YES[@]}")
if [[ -n "$IMAGE" ]]; then CHILD+=(--image "$IMAGE"); fi
if $SET_GH_VARS; then CHILD+=(--set-gh-vars); fi
for s in "${ONLY_SERVERS[@]}"; do CHILD+=(--servers "$s"); done

# ------------------------------------------------------------------ run
typeset -A RESULT DURATION
total=${#RUN}
n=0
for step in "${RUN[@]}"; do
  n=$(( n + 1 ))
  print -u2 -r -- ""
  print -u2 -r -- "${_b}${_c}▶ [$n/$total] $step${_o}  ${_d}${DESC[$step]}${_o}"
  if $STEPWISE && $INTERACTIVE; then
    read -r "ans?${_c}  run it? [Y]es / [n]o, skip / [q]uit ${_o}"
    case "${ans:-y}" in
      [nN]*) RESULT[$step]=skipped; log_info "skipped $step"; continue ;;
      [qQ]*) break ;;
    esac
  fi
  while true; do
    t0=$SECONDS
    zsh "$0" "${CHILD[@]}" --_step "$step" && rc=0 || rc=$?
    DURATION[$step]=$(( SECONDS - t0 ))
    if (( rc == 0 )); then
      RESULT[$step]=done
      state_set "$step" done "${DURATION[$step]}"
      print -u2 -r -- "${_g}✓ $step${_o} ${_d}($(fmt_duration ${DURATION[$step]}))${_o}"
      break
    fi
    RESULT[$step]=failed
    state_set "$step" failed "${DURATION[$step]}"
    print -u2 -r -- "${_r}✗ $step failed (exit $rc)${_o}"
    if $INTERACTIVE; then
      read -r "ans?${_c}  [r]etry, [s]kip, [q]uit? [q]${_o} "
      case "${ans:-q}" in
        [rR]*) continue ;;
        [sS]*) RESULT[$step]=skipped; break ;;
      esac
    fi
    break 2
  done
done

# ------------------------------------------------------------------ summary
print -u2 -r -- ""
line
print -u2 -r -- "${_b}Summary${_o}"
failed_step=""
for step in "${RUN[@]}"; do
  case "${RESULT[$step]:-not run}" in
    done)    print -u2 -r -- "  ${_g}✓${_o} $(printf '%-12s' $step) ${_d}$(fmt_duration ${DURATION[$step]:-0})${_o}" ;;
    failed)  print -u2 -r -- "  ${_r}✗${_o} $(printf '%-12s' $step) ${_r}failed${_o}"; failed_step="$step" ;;
    skipped) print -u2 -r -- "  ${_y}↷${_o} $(printf '%-12s' $step) ${_y}skipped${_o}" ;;
    *)       print -u2 -r -- "  ${_d}·${_o} $(printf '%-12s' $step) ${_d}not run${_o}" ;;
  esac
done
line
if [[ -n "$failed_step" ]]; then
  print -u2 -r -- "${_r}Stopped at $failed_step.${_o} Fix the error above, then resume with:"
  print -u2 -r -- "  ${_b}$0 --config $CONFIG_DIR --resume${_o}   ${_d}(or just rerun it on a terminal)${_o}"
  exit 1
fi

if [[ -n "${RESULT[servers]:-}${RESULT[smoke]:-}" ]]; then
  load_platform
  DOMAIN="$(env_default_domain 2>/dev/null || true)"
  if [[ -n "$DOMAIN" ]]; then
    print -u2 -r -- "${_b}Reachable now (before DNS)${_o}"
    "$CHAT_PY" "$CHAT_RENDER" urls "$CONFIG_DIR" --default-domain "$DOMAIN" >&2
  fi
  cat >&2 <<EOT

${_b}Next${_o}
  * Sign in at the "now" URLs above with your Princeton account. Each realm's owner in
    chat.yml has full rights there on first sign-in (see "Accounts and roles" in the README).
  * Commit and push the config repo; CI now deploys every change to chat.yml.
  * When OIT has created the records in dns-request-$DEPT.md, for each server:
      $S/bind-domain.zsh --config $CONFIG_DIR --server <name> --wait 30
    then set dns: live for it in chat.yml, render, rerun entra-app.zsh, commit and push.
EOT
fi
exit 0
