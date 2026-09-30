#!/usr/bin/env zsh
# local.zsh — run one server from a chat.yml on this machine and open it in a browser.
# No Azure and no Entra: the real image, with the department's rendered settings, against
# local Postgres/sidecars, a mock Entra (signs you in as the realm owner) and a mail sink.
#
#   scripts/local.zsh up [--config <dept-dir>] [--server <name>] [--theme <name|default>]
#   scripts/local.zsh status | logs | trust | down
#
#   --config  a department directory holding chat.yml (default: the e2e fixture)
#   --server  which server to run (default: dept, else the first); one at a time
#   --theme   override the server's theme, e.g. to compare paper-tiger with default
#
# Every host becomes <host>.localhost (chat.orfe.princeton.edu -> chat.orfe.princeton.edu.localhost),
# which macOS resolves to 127.0.0.1 by itself. TLS comes from a local CA: accept the
# browser warning, or run `local.zsh trust` once to add that CA to your login keychain.
# Ports 443, 9080 (mock Entra) and 8025 (mail UI) on 127.0.0.1 must be free. Rerunning
# `up` keeps data; `down` deletes it.
source "${0:A:h}/common.zsh"

STATE="${CHAT_LOCAL_STATE:-$CHAT_ROOT/.local-stack}"
PROJECT=chat-local
here="$CHAT_ROOT/tests/e2e"
compose=(docker-compose -p "$PROJECT" -f "$here/docker-compose.yml" -f "$here/local.yml" -f "$STATE/override.json")
export CHAT_E2E_STATE="$STATE"

(( $# )) || die "usage: local.zsh up|status|logs|trust|down [options]"
ACTION="$1"; shift
CONFIG="$here/fixture/e2e" SERVER="" THEME=""
while (( $# )); do
  case "$1" in
    --config) CONFIG="${2:?--config needs a directory}"; shift 2 ;;
    --server) SERVER="${2:?--server needs a name}"; shift 2 ;;
    --theme) THEME="${2:?--theme needs a name}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ "$ACTION" == up || -z "$SERVER$THEME" && "$CONFIG" == "$here/fixture/e2e" ]] \
  || die "--config/--server/--theme only apply to up"

running() { [[ -f "$STATE/override.json" && -n "$("${compose[@]}" ps -q zulip 2>/dev/null)" ]]; }

show_urls() {
  local row
  log_step "Local stack: $(jq -r .config "$STATE/meta.json"), server $(jq -r .server "$STATE/stack.json")"
  for row in "${(@f)$(jq -c '.realms[]' "$STATE/stack.json")}"; do
    log_ok "$(jq -r '"\(.name): \(.url)  (sign in with Microsoft = \(.owner.email), owner)"' <<<"$row")"
  done
  log_info "mail Zulip sends: http://localhost:8025"
  log_info "stop: scripts/local.zsh down   (deletes the local data)"
}

case "$ACTION" in
  down)
    require_cmd docker-compose
    if [[ -f "$STATE/override.json" ]]; then
      "${compose[@]}" --profile jobs down -v --remove-orphans
    fi
    rm -rf "$STATE"
    log_ok "local stack removed"
    exit 0 ;;
  status)
    running || die "the local stack is not running (scripts/local.zsh up)"
    "${compose[@]}" ps
    show_urls
    exit 0 ;;
  logs)
    running || die "the local stack is not running (scripts/local.zsh up)"
    exec "${compose[@]}" logs -f --tail 100 zulip edge ;;
  trust)
    require_cmd security
    running || die "start the stack first: the CA is created by its first TLS request"
    ca="$STATE/local-ca.crt"
    "${compose[@]}" cp edge:/data/caddy/pki/authorities/local/root.crt "$ca" \
      || die "no local CA yet; open one of the URLs once, then rerun trust"
    log_info "adding $ca to your login keychain as a trusted root (macOS asks for your password)"
    security add-trusted-cert -r trustRoot -k "$HOME/Library/Keychains/login.keychain-db" "$ca"
    log_ok "trusted; Safari and Chrome stop warning (Firefox keeps its own store). down deletes this CA, so remove it from Keychain Access afterwards"
    exit 0 ;;
  up) ;;
  *) die "unknown action: $ACTION (up|status|logs|trust|down)" ;;
esac

require_cmd docker docker-compose jq openssl lsof
require_python
CONFIG="${CONFIG:A}"
[[ -f "$CONFIG/chat.yml" ]] || die "$CONFIG/chat.yml not found"
meta="$(jq -cn --arg c "$CONFIG" --arg s "$SERVER" --arg t "$THEME" '{config: $c, server: $s, theme: $t}')"

if running; then
  [[ "$(jq -c . "$STATE/meta.json")" == "$meta" ]] \
    || die "the local stack is running $(jq -r .config "$STATE/meta.json") (server $(jq -r .server "$STATE/stack.json")) with other options; run scripts/local.zsh down first"
  log_ok "already running; reconciling"
else
  for port in 443 9080 8025; do
    if lsof -nP -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then
      die "port $port is in use on this machine (lsof -nP -iTCP:$port -sTCP:LISTEN)"
    fi
  done
fi

log_step "Rendering $CONFIG for local use"
mkdir -p "$STATE/secrets"
chmod 700 "$STATE"
for s in secret_key postgres_password redis_password rabbitmq_password memcached_password social_auth_oidc_secret email_password; do
  [[ -s "$STATE/secrets/zulip__$s" ]] || { umask 077; openssl rand -hex 24 | tr -d '\n' > "$STATE/secrets/zulip__$s"; }
done
# Fixed by tests/pg (the Azure-like admin role); the mock ping key is never used.
print -rn -- admin-test-password > "$STATE/secrets/pg_admin_password"
print -rn -- local-unused > "$STATE/secrets/hc_ping_key"

localize=("$CHAT_PY" "$CHAT_ROOT/tools/local_stack.py" localize "$CONFIG" "$STATE/config")
[[ -n "$SERVER" ]] && localize+=(--server "$SERVER")
[[ -n "$THEME" ]] && localize+=(--theme "$THEME")
stack="$("${localize[@]}")"
print -r -- "$stack" | jq . > "$STATE/stack.json"
print -r -- "$meta" > "$STATE/meta.json"
server_json="$(jq -r .server_json "$STATE/stack.json")"
"$CHAT_PY" "$CHAT_RENDER" resolve --server "$server_json" --default-domain local.invalid \
  --image pu-shd-chat:e2e --registry localchatacr.azurecr.io --allow-unpinned-image > "$STATE/params.json" 2>/dev/null \
  || die "render.py resolve failed: rerun it by hand for the error"
override() { "$CHAT_PY" "$CHAT_ROOT/tools/local_stack.py" override --params "$STATE/params.json" \
  --server-json "$server_json" --subnet "$1" --state "$STATE"; }
log_ok "server $(jq -r .server "$STATE/stack.json"): $(jq -r '.hosts | join(", ")' "$STATE/stack.json")"

log_step "Building the image from this checkout"
net_subnet() { docker network inspect "${PROJECT}_e2e" -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true; }
subnet="$(net_subnet)"
if [[ -z "$subnet" ]]; then
  override 127.0.0.1/32  # compose needs the file before the network (and its subnet) exists
  "${compose[@]}" up --no-start zulip edge
  subnet="$(net_subnet)"
fi
"${compose[@]}" build zulip
[[ "$subnet" == */* ]] || die "could not read the ${PROJECT}_e2e network subnet"
override "$subnet"

log_step "Starting (first boot runs Zulip's setup and migrations: a few minutes)"
"${compose[@]}" up -d zulip edge mailpit
cid="$("${compose[@]}" ps -q zulip)"
health=missing
for i in {1..90}; do
  health="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || print missing)"
  [[ "$health" == healthy ]] && break
  if [[ "$health" == unhealthy || "$(docker inspect -f '{{.State.Status}}' "$cid")" == exited ]]; then
    "${compose[@]}" logs --tail 60 zulip >&2 || true
    die "zulip is $health"
  fi
  (( i % 6 )) || log_info "still starting ($health)"
  sleep 10
done
[[ "$health" == healthy ]] || die "zulip not healthy after 15 minutes (scripts/local.zsh logs)"
log_ok "zulip healthy"

log_step "Realms"
for row in "${(@f)$(jq -c '.realms[]' "$STATE/stack.json")}"; do
  slug="$(jq -r .slug <<<"$row")"
  out="$("${compose[@]}" run --rm -T mgmt chat:manage ensure-realm "${slug:-_root}" \
    "$(jq -r .name <<<"$row")" "$(jq -r .owner.email <<<"$row")" "$(jq -r .owner.name <<<"$row")" 2>&1)" \
    || { print -u2 -r -- "$out" | tail -30; die "ensure-realm ${slug:-_root} failed"; }
  res="$(print -r -- "$out" | sed -n 's/.*CHAT-RESULT: //p' | tail -n 1)"
  jq -e '.ok == true' <<<"$res" >/dev/null 2>&1 || { print -u2 -r -- "$out" | tail -30; die "ensure-realm ${slug:-_root} did not report ok"; }
  log_ok "${slug:-(root)}: $(jq -r 'if .created then "created" else "present" end' <<<"$res")"
done

show_urls
