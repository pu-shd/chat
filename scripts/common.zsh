# common.zsh — sourced by every pu-shd/chat operations script (macOS zsh and CI).
#
# Conventions (after graddb/meet):
#   * idempotent: every step checks before it creates;
#   * no secret defaults, never overwrite a live secret with an empty value, never put a
#     secret on a command line (argv is world-readable) — values go through 0600 files;
#   * every script takes --config <dept-dir> (a directory holding chat.yml and the
#     rendered generated/) and, where it acts on one server, --server <name>;
#   * failures are loud: nothing here treats "no output" as success.

setopt errexit nounset pipefail no_unset extended_glob

CHAT_ROOT="${CHAT_ROOT:-${${(%):-%x}:A:h:h}}"
CHAT_PY="${CHAT_PY:-$CHAT_ROOT/.venv/bin/python}"
CHAT_RENDER="$CHAT_ROOT/tools/render.py"
CHAT_API_VERSION="${CHAT_API_VERSION:-2025-01-01}"

if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  _c_red=$'\e[0;31m' _c_grn=$'\e[0;32m' _c_yel=$'\e[1;33m' _c_blu=$'\e[0;34m' _c_off=$'\e[0m'
else
  _c_red='' _c_grn='' _c_yel='' _c_blu='' _c_off=''
fi
log_info()  { print -u2 -r -- "${_c_blu}[INFO]${_c_off} $*"; }
log_ok()    { print -u2 -r -- "${_c_grn}[ OK ]${_c_off} $*"; }
log_warn()  { print -u2 -r -- "${_c_yel}[WARN]${_c_off} $*"; }
log_error() { print -u2 -r -- "${_c_red}[FAIL]${_c_off} $*"; }
log_step()  { print -u2 -r -- ""; print -u2 -r -- "${_c_blu}==> $*${_c_off}"; }
die() { log_error "$1"; exit "${2:-1}"; }
# errexit ends a script on an unexpected failure; say where, so no exit is silent.
TRAPZERR() {
  local rc=$?
  print -u2 -r -- "${_c_red}[FAIL]${_c_off} unexpected exit $rc at ${funcfiletrace[1]:-${0}}"
  return $rc
}

# Markdown into the GitHub step summary when running in Actions; stderr otherwise.
summary() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    print -r -- "$*" >> "$GITHUB_STEP_SUMMARY"
  fi
}
# ::warning:: annotations make fallbacks visible on the run page, not just in logs.
gh_warning() {
  log_warn "$*"
  # stderr: stdout is often data being captured (e.g. ip-gate --emit); the runner reads both.
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then print -u2 -r -- "::warning::$*"; fi
  summary "> ⚠️ $*"
  return 0
}

require_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "required command not found: $c"
  done
}

require_python() {
  [[ -x "$CHAT_PY" ]] || die "Python environment missing at $CHAT_PY — run $CHAT_ROOT/scripts/setup-venv.zsh"
}

# ------------------------------------------------------------------ arguments

CONFIG_DIR="" SERVER="" ASSUME_YES=false DRY_RUN=false
typeset -ga CHAT_ARGS_REST
parse_common_args() {
  CHAT_ARGS_REST=()
  while (( $# )); do
    case "$1" in
      --config) CONFIG_DIR="${2:?--config needs a directory}"; shift 2 ;;
      --server) SERVER="${2:?--server needs a name}"; shift 2 ;;
      --yes|-y) ASSUME_YES=true; shift ;;
      --dry-run) DRY_RUN=true; shift ;;
      *) CHAT_ARGS_REST+=("$1"); shift ;;
    esac
  done
  [[ -n "$CONFIG_DIR" ]] || die "--config <dept-dir> is required (the directory holding chat.yml)"
  CONFIG_DIR="${CONFIG_DIR:A}"
  [[ -f "$CONFIG_DIR/chat.yml" ]] || die "$CONFIG_DIR/chat.yml not found"
}

confirm() {
  # confirm "question"  — y/N; --yes answers yes; non-interactive without --yes answers no.
  $ASSUME_YES && return 0
  [[ -t 0 ]] || { log_warn "non-interactive and no --yes: declining '$1'"; return 1; }
  local reply
  read -r "reply?$1 [y/N] "
  [[ "$reply" == [yY]* ]]
}

confirm_typed() {
  # confirm_typed "EXACT PHRASE" — for destructive actions. CHAT_CONFIRM supplies the
  # phrase non-interactively (the teardown workflow's typed input); --yes does NOT.
  local phrase="$1" reply="${CHAT_CONFIRM:-}"
  if [[ -z "$reply" ]]; then
    [[ -t 0 ]] || die "refusing: type-to-confirm needed; set CHAT_CONFIRM='$phrase'"
    read -r "reply?Type '$phrase' to continue: "
  fi
  [[ "$reply" == "$phrase" ]] || die "confirmation did not match '$phrase'; nothing was changed"
}

# ------------------------------------------------------------------ rendered config

typeset -g PLATFORM_JSON SERVER_JSON
typeset -g RG LOCATION SUB_ID TENANT_ID PREFIX ENV_NAME KV_NAME ST_NAME PG_NAME ID_NAME LAW_NAME
typeset -g APP_NAME MGMT_JOB DBINIT_JOB

# Read a required field. Only a missing (null) value is an error: plain `jq -e` would
# also fail on `false`, and under errexit that silently ends the script.
_jq_req() { jq -r "$1 | if . == null then error(\"missing field: $1\") else . end" "$2"; }
jqp() { _jq_req "$1" "$PLATFORM_JSON"; }
jqs() { _jq_req "$1" "$SERVER_JSON"; }

load_platform() {
  require_cmd jq
  require_python
  if [[ "${CHAT_SKIP_RENDER_CHECK:-}" != 1 ]]; then
    "$CHAT_PY" "$CHAT_RENDER" render "$CONFIG_DIR" --check >/dev/null \
      || die "$CONFIG_DIR/generated is stale; run: $CHAT_PY $CHAT_RENDER render $CONFIG_DIR"
  fi
  PLATFORM_JSON="$CONFIG_DIR/generated/platform.json"
  [[ -f "$PLATFORM_JSON" ]] || die "$PLATFORM_JSON missing; render first"
  RG="$(jqp .resource_group)"
  LOCATION="$(jqp .region)"
  SUB_ID="$(jqp .subscription_id)"
  TENANT_ID="$(jqp .tenant_id)"
  PREFIX="$(jqp .prefix)"
  ENV_NAME="$(jqp .names.environment)"
  KV_NAME="$(jqp .names.key_vault)"
  ST_NAME="$(jqp .names.storage)"
  PG_NAME="$(jqp .names.postgres)"
  ID_NAME="$(jqp .names.identity)"
  LAW_NAME="$(jqp .names.log_analytics)"
}

load_server() {
  local name="${1:-$SERVER}"
  [[ -n "$name" ]] || die "--server <name> is required"
  SERVER_JSON="$CONFIG_DIR/generated/servers/$name.json"
  [[ -f "$SERVER_JSON" ]] || die "no server '$name' in $CONFIG_DIR/generated/servers ($(ls "$CONFIG_DIR/generated/servers" 2>/dev/null | sed 's/\.json$//' | tr '\n' ' '))"
  SERVER="$name"
  APP_NAME="$(jqs .app_name)"
  MGMT_JOB="$(jqs .jobs.mgmt)"
  DBINIT_JOB="$(jqs .jobs.dbinit)"
}

server_names() { jq -er '.servers[]' "$CONFIG_DIR/generated/index.json"; }

# ------------------------------------------------------------------ azure

az_login() {
  require_cmd az
  if ! az account show >/dev/null 2>&1; then
    [[ -t 0 ]] || die "not logged in to Azure (CI must run azure/login first)"
    log_info "Not logged in to Azure; running az login"
    az login --tenant "$TENANT_ID" >/dev/null
  fi
  az account set --subscription "$SUB_ID"
  local tenant
  tenant="$(az account show --query tenantId -o tsv)"
  [[ "$tenant" == "$TENANT_ID" ]] || die "signed in to tenant $tenant, config expects $TENANT_ID"
}

# signed_in_object_id — the Entra object id of whoever az is signed in as: the user, or
# the service principal of the logged-in client id (CI).
typeset -g CHAT_ME_OID=""
signed_in_object_id() {
  if [[ -z "$CHAT_ME_OID" ]]; then
    if [[ "$(az account show --query user.type -o tsv)" == user ]]; then
      CHAT_ME_OID="$(az_retry ad signed-in-user show --query id -o tsv)"
    else
      CHAT_ME_OID="$(az_retry ad sp show --id "$(az account show --query user.name -o tsv)" --query id -o tsv)"
    fi
  fi
  [[ -n "$CHAT_ME_OID" ]] || { log_error "could not read the signed-in principal's object id"; return 2; }
  print -r -- "$CHAT_ME_OID"
}

# entra_app_by_name <display-name> — the appId of THE app registration with that display
# name, or nothing if there is none. Display names are not unique and anyone in the tenant
# can register one, so a match is used only if it is the only one AND the signed-in
# principal is among its owners (the scripts' own apps have their creator as owner).
# Otherwise it explains and returns 2: callers decide whether that ends the script
# (`APP_ID="$(entra_app_by_name X)" || die ...`). It never guesses.
entra_app_by_name() {
  local name="$1" raw me
  typeset -a ids owners
  raw="$(az_retry ad app list --display-name "$name" --query '[].appId' -o tsv)" \
    || { log_error "could not list app registrations named $name"; return 2; }
  ids=("${(@f)raw}"); ids=("${(@)ids:#}")
  (( ${#ids} )) || return 0
  if (( ${#ids} > 1 )); then
    log_error "${#ids} app registrations are named $name (${(j:, :)ids}); refusing to pick one. Delete the impostor(s) (az ad app delete --id <appId>) or rename them"
    return 2
  fi
  me="$(signed_in_object_id)" || return 2
  raw="$(az_retry ad app owner list --id "${ids[1]}" --query '[].id' -o tsv)" \
    || { log_error "could not list the owners of $name (${ids[1]})"; return 2; }
  owners=("${(@f)raw}")
  if (( ! ${owners[(Ie)$me]} )); then
    log_error "app registration $name (${ids[1]}) is not owned by you ($me), so it may not be ours: if it is, add yourself as an owner (az ad app owner add --id ${ids[1]} --owner-object-id $me, by a current owner or an Entra admin); if it is not, delete it or have it renamed"
    return 2
  fi
  print -r -- "${ids[1]}"
}

# az_retry <az args...> — retries only what is transient (after meet's ci.yml).
# Server-side throttling/conflicts are always safe to retry: Azure refused the request.
# A client-side timeout or connection reset is not, for `containerapp job start`: the
# request may have reached Azure, and retrying could start a second execution.
az_retry() {
  local attempt=1 max="${AZ_RETRY_MAX:-5}" out rc errf
  local transient='OperationInProgress|ContainerAppOperationInProgress|TooManyRequests|Too Many Requests|(^|[^0-9])429([^0-9]|$)|temporarily unavailable|RetryableError'
  [[ "${1:-} ${2:-} ${3:-}" == "containerapp job start" ]] || transient+='|Connection reset|Connection aborted|timed out'
  errf="$(mktemp)"
  while true; do
    # stdout only is returned: az warnings (e.g. "a new Bicep release") must not end up
    # inside JSON a caller parses.
    out="$(az "$@" 2>"$errf")" && rc=0 || rc=$?
    if (( rc == 0 )); then
      grep -v 'new Bicep release' "$errf" >&2 || true
      rm -f "$errf"
      print -r -- "$out"
      return 0
    fi
    if (( attempt >= max )) || ! grep -qiE "$transient" "$errf"; then
      cat "$errf" >&2
      rm -f "$errf"
      return $rc
    fi
    log_warn "az $1 $2: transient failure (attempt $attempt/$max); retrying"
    sleep $(( attempt * ${AZ_RETRY_SLEEP:-10} ))
    attempt=$(( attempt + 1 ))
  done
}

# az_exists <az show args...> — 0 if the resource exists, 1 if Azure says it does not.
# Any other failure (auth, network, throttling that outlasted az_retry) ends the script:
# a "missing" guessed from an error would make a caller recreate, overwrite or forget
# something that is live (e.g. treat a running server as a first deploy).
AZ_NOT_FOUND='ResourceNotFound|ResourceGroupNotFound|NotFound\)|was not found|could not be found|does not exist'
az_exists() {
  local errf rc=0
  errf="$(mktemp)"
  az_retry "$@" >/dev/null 2>"$errf" || rc=$?
  if (( rc == 0 )); then rm -f "$errf"; return 0; fi
  if grep -qiE "$AZ_NOT_FOUND" "$errf"; then rm -f "$errf"; return 1; fi
  cat "$errf" >&2
  rm -f "$errf"
  die "az ${1:-} ${2:-} ${3:-} failed (exit $rc) without saying the resource is missing; refusing to guess"
}

kv_secret_exists() {
  az_exists keyvault secret show --vault-name "$KV_NAME" --name "$1" --query id -o tsv
}

kv_secret_get() {
  az keyvault secret show --vault-name "$KV_NAME" --name "$1" --query value -o tsv
}

# kv_secret_set_from_stdin <name> [extra az args...] — value on stdin, via a 0600 file,
# never argv. Extra args (e.g. --expires 2028-09-30T00:00:00Z) pass through to az.
kv_secret_set_from_stdin() {
  local name="$1" tmp rc=0
  shift
  # Before the plaintext file exists: this can end the script, which skips `always`.
  kv_recover_if_deleted "$name"
  tmp="$(mktemp)"
  # errexit and exit skip an `always` block (zsh), so failures inside are caught with
  # `|| rc=$?` and the plaintext file is removed on every path.
  {
    chmod 600 "$tmp" || rc=$?
    # One trailing newline (from `print`/`echo`/az tsv) is never part of a secret.
    (( rc )) || "$CHAT_PY" -c 'import sys; d = sys.stdin.buffer.read(); sys.stdout.buffer.write(d[:-1] if d.endswith(b"\n") else d)' > "$tmp" || rc=$?
    if (( rc == 0 )) && [[ ! -s "$tmp" ]]; then rc=-1; fi
    (( rc )) || az keyvault secret set --vault-name "$KV_NAME" --name "$name" --file "$tmp" --encoding utf-8 "$@" --output none || rc=$?
  } always {
    rm -f "$tmp"
  }
  (( rc != -1 )) || die "refusing to store an empty value in Key Vault secret $name"
  (( rc == 0 )) || die "could not store Key Vault secret $name (exit $rc)"
}

random_secret() { print -rn -- "$(openssl rand -hex "${1:-32}")"; }

# kv_recover_if_deleted <name> — with purge protection a deleted secret cannot be
# re-created under its name for 90 days, only recovered.
kv_recover_if_deleted() {
  if az keyvault secret show-deleted --vault-name "$KV_NAME" --name "$1" --query recoveryId -o tsv >/dev/null 2>&1; then
    log_warn "Key Vault secret $1 was deleted; recovering it"
    az keyvault secret recover --vault-name "$KV_NAME" --name "$1" --output none
    local i
    for i in {1..12}; do kv_secret_exists "$1" && return 0; sleep 5; done
    die "recovered $1 but it did not reappear"
  fi
  return 0
}

# kv_secret_ensure_random <name> — generate once; an existing value is never replaced.
kv_secret_ensure_random() {
  local name="$1"
  kv_recover_if_deleted "$name"
  if kv_secret_exists "$name"; then
    log_ok "Key Vault secret $name present"
  else
    random_secret | kv_secret_set_from_stdin "$name"
    log_ok "Key Vault secret $name generated"
  fi
}

env_default_domain() {
  az containerapp env show -g "$RG" -n "$ENV_NAME" --query properties.defaultDomain -o tsv
}

env_verification_id() {
  az containerapp env show -g "$RG" -n "$ENV_NAME" --query properties.customDomainConfiguration.customDomainVerificationId -o tsv
}

# A transient az error must not read as "no app": deploy-server would treat a live server
# as a first deploy (dropping its custom domains and skipping the maintenance window).
app_exists() { az_exists containerapp show -g "$RG" -n "$APP_NAME" --query id -o tsv; }

# run_job <job> <container> [args...] — start a manual job execution, wait for it and
# print the JSON from its last "CHAT-RESULT:" log line. Fails if the execution fails,
# times out, never reports a result, or the result does not satisfy $RUN_JOB_EXPECT
# (a jq filter, e.g. '.dropped == true'). $RUN_JOB_ENV (array of K=V) overrides env vars.
typeset -ga RUN_JOB_ENV
RUN_JOB_ENV=()
run_job() {
  local job="$1" container="$2"; shift 2
  local a
  for a in "$@"; do
    # az would parse "-..." as its own option (e.g. --image): never let data do that.
    [[ "$a" != -* ]] || die "job $job: refusing argument that starts with '-': $a"
  done
  local start_args=(--name "$job" --resource-group "$RG" --container-name "$container")
  (( $# )) && start_args+=(--args "$@")
  (( ${#RUN_JOB_ENV} )) && start_args+=(--env-vars "${RUN_JOB_ENV[@]}")
  local exec_name exec_status waited=0 timeout="${JOB_TIMEOUT:-1800}"
  exec_name="$(az_retry containerapp job start "${start_args[@]}" --query name -o tsv)"
  [[ -n "$exec_name" ]] || die "job $job: no execution name returned"
  log_info "job $job: execution $exec_name started"
  while true; do
    exec_status="$(az containerapp job execution show --name "$job" --resource-group "$RG" \
      --job-execution-name "$exec_name" --query properties.status -o tsv 2>/dev/null || true)"
    case "$exec_status" in
      Succeeded|Failed|Stopped|Degraded) break ;;
    esac
    if (( waited >= timeout )); then
      die "job $job: execution $exec_name still '$exec_status' after ${timeout}s"
    fi
    sleep "${JOB_POLL:-15}"
    waited=$(( waited + ${JOB_POLL:-15} ))
  done
  local logs result
  logs="$(job_logs "$job" "$exec_name" "$container")"
  result="$(print -r -- "$logs" | sed -n 's/.*CHAT-RESULT: //p' | tail -n 1)"
  if [[ "$exec_status" != Succeeded ]]; then
    print -u2 -r -- "$logs" | tail -n 40
    die "job $job: execution $exec_name ended $exec_status${result:+ — $result}"
  fi
  [[ -n "$result" ]] || { print -u2 -r -- "$logs" | tail -n 40; die "job $job: succeeded but reported no CHAT-RESULT"; }
  print -r -- "$result" | jq -e '.ok == true' >/dev/null || die "job $job: $result"
  if [[ -n "${RUN_JOB_EXPECT:-}" ]]; then
    print -r -- "$result" | jq -e "$RUN_JOB_EXPECT" >/dev/null || die "job $job: result $result does not satisfy $RUN_JOB_EXPECT"
  fi
  print -r -- "$result"
}

job_logs() {
  local job="$1" exec_name="$2" container="$3" out tries=0
  # Console logs can lag the execution status; retry before falling back to Log Analytics.
  while (( tries < ${JOB_LOG_TRIES:-6} )); do
    out="$(az containerapp job logs show --name "$job" --resource-group "$RG" \
      --execution "$exec_name" --container "$container" --format text --tail 300 2>/dev/null || true)"
    if print -r -- "$out" | grep -q 'CHAT-RESULT:'; then
      print -r -- "$out"; return 0
    fi
    tries=$(( tries + 1 ))
    sleep "${JOB_LOG_SLEEP:-10}"
  done
  # The replica may be gone; Log Analytics ingestion lags a few minutes, so keep asking.
  local law la tries_la=0
  law="$(az monitor log-analytics workspace show -g "$RG" -n "$LAW_NAME" --query customerId -o tsv 2>/dev/null || true)"
  if [[ -n "$law" ]]; then
    while (( tries_la < ${JOB_LA_TRIES:-20} )); do
      la="$(az monitor log-analytics query --workspace "$law" --analytics-query \
        "ContainerAppConsoleLogs_CL | where ContainerGroupName_s startswith '$exec_name' | order by TimeGenerated asc | project Log_s" \
        --query '[].Log_s' -o tsv 2>/dev/null || true)"
      if print -r -- "$la" | grep -q 'CHAT-RESULT:'; then print -r -- "$la"; return 0; fi
      tries_la=$(( tries_la + 1 ))
      sleep "${JOB_LOG_SLEEP:-30}"
    done
  fi
  print -r -- "$out"
}

# ------------------------------------------------------------------ healthchecks

hc_enabled() { [[ "$(jq -r '.healthchecks.enabled' "$PLATFORM_JSON")" == true ]]; }

# hc_ping_key — HEALTHCHECKS_PING_KEY (CI secret) or Key Vault healthchecks-ping-key.
hc_ping_key() {
  if [[ -n "${HEALTHCHECKS_PING_KEY:-}" ]]; then print -rn -- "$HEALTHCHECKS_PING_KEY"; return 0; fi
  az keyvault secret show --vault-name "$KV_NAME" --name healthchecks-ping-key --query value -o tsv 2>/dev/null
}

# hc_ping <slug> <success|fail|start> [message] — never fails the caller, but a ping
# that cannot be sent while healthchecks are enabled is a visible warning.
hc_ping() {
  local slug="$1" kind="${2:-success}" msg="${3:-}" key base suffix=""
  hc_enabled || return 0
  key="$(hc_ping_key || true)"
  if [[ -z "$key" ]]; then
    gh_warning "healthchecks enabled but no ping key (HEALTHCHECKS_PING_KEY / Key Vault healthchecks-ping-key); $slug not pinged"
    return 0
  fi
  base="$(jq -r '.healthchecks.ping_base' "$PLATFORM_JSON")"
  case "$kind" in fail) suffix=/fail ;; start) suffix=/start ;; success) ;; *) suffix="/$kind" ;; esac
  # The URL contains the ping key: hand it to curl as config on stdin, not argv.
  if print -r -- "url = \"$base/$key/$slug$suffix?create=1\"" \
       | curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "${msg:-$kind}" -K -; then
    log_info "healthchecks: pinged $slug ($kind)"
  else
    gh_warning "healthchecks: ping to $slug failed"
  fi
  return 0
}
