#!/usr/bin/env zsh
# run.zsh — boot the real Zulip image with settings rendered from a chat.yml and test it
# end to end (health, OIDC sign-in against a mock Entra, redirects, realm creation via
# the mgmt job, outgoing mail).
#
#   tests/e2e/run.zsh            (KEEP=1 leaves the stack up for poking at it)
setopt errexit nounset pipefail
here="${0:A:h}" root="${0:A:h:h:h}"
state="$here/.state"
py="${CHAT_PY:-$root/.venv/bin/python}"
[[ -x "$py" ]] || { print -u2 "run $root/scripts/setup-venv.zsh first"; exit 1; }

rm -rf "$state"; mkdir -p "$state/secrets"
cp -R "$here/fixture/e2e" "$state/e2e"
"$py" "$root/tools/render.py" render "$state/e2e" 2>/dev/null
"$py" "$root/tools/render.py" resolve --server "$state/e2e/generated/servers/dept.json" \
  --default-domain e2e.invalid --image pu-shd-chat:e2e --allow-unpinned-image > "$state/dept.params.json"

for s in secret_key postgres_password redis_password rabbitmq_password memcached_password social_auth_oidc_secret email_password; do
  openssl rand -hex 24 | tr -d '\n' > "$state/secrets/zulip__$s"
done
print -rn -- admin-test-password > "$state/secrets/pg_admin_password"
print -rn -- e2e-ping-key > "$state/secrets/hc_ping_key"

# The rendered environment, with only the host names swapped for compose services.
write_override() {  # write_override <subnet>
"$py" - "$state/dept.params.json" "$1" > "$state/override.json" <<'PY'
import json, sys
subnet = sys.argv[2]
params = json.load(open(sys.argv[1]))["parameters"]
env = {e["name"]: e["value"] for e in params["zulipEnv"]["value"]}
entra = "https://login.microsoftonline.com/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/v2.0"
swap = {
    "SETTING_REMOTE_POSTGRES_HOST": ("e2e-chat-pg.postgres.database.azure.com", "postgres"),
    "SETTING_REMOTE_POSTGRES_SSLMODE": ("require", "disable"),
    "SETTING_REDIS_HOST": ("127.0.0.1", "redis"),
    "SETTING_RABBITMQ_HOST": ("127.0.0.1", "rabbitmq"),
    "SETTING_MEMCACHED_LOCATION": ("127.0.0.1:11211", "memcached:11211"),
    "SETTING_EMAIL_HOST": ("smtp.resend.com", "mailpit"),
    "SETTING_EMAIL_PORT": ("587", "1025"),
    "SETTING_EMAIL_USE_TLS": ("True", "False"),
    "LOADBALANCER_IPS": ("10.60.0.0/23", subnet),
}
for key, (expect, new) in swap.items():
    assert expect in env[key], f"rendered {key}={env[key]!r} no longer contains {expect!r}"
    env[key] = new
assert entra in env["SETTING_SOCIAL_AUTH_OIDC_ENABLED_IDPS"]
env["SETTING_SOCIAL_AUTH_OIDC_ENABLED_IDPS"] = env["SETTING_SOCIAL_AUTH_OIDC_ENABLED_IDPS"].replace(entra, "http://oidc:8080/entra")
# Zulip sends outgoing HTTP through smokescreen, which refuses private addresses; the
# mock IdP is one (login.microsoftonline.com in production is not). E2E only.
env["CONFIG_http_proxy__allow_ranges"] = subnet
assert not any("$" in v for v in env.values()), "compose would interpolate $"
mgmt = {**env, "AUTO_BACKUP_ENABLED": "False"}
json.dump({"services": {"zulip": {"environment": env}, "mgmt": {"environment": mgmt}}}, sys.stdout, indent=2)
PY
}
# The proxy trust range is the compose network's; Docker picks it, so ask after creating it.
write_override 127.0.0.1/32

compose=(docker-compose -p chat-e2e -f "$here/docker-compose.yml" -f "$state/override.json")
cleanup() {
  local rc=$?
  if (( rc != 0 )); then
    print -u2 -r -- "---- zulip logs (tail) ----"
    "${compose[@]}" logs --tail 80 zulip >&2 || true
  fi
  [[ -n "${KEEP:-}" ]] || "${compose[@]}" --profile jobs down -v --remove-orphans >/dev/null 2>&1 || true
  exit $rc
}
trap cleanup EXIT

"${compose[@]}" build zulip runner
"${compose[@]}" up --no-start zulip
subnet="$(docker network inspect chat-e2e_e2e -f '{{(index .IPAM.Config 0).Subnet}}')"
[[ "$subnet" == */* ]] || { print -u2 "could not read the e2e network subnet"; exit 1; }
write_override "$subnet"
print -u2 -r -- "e2e network $subnet (LOADBALANCER_IPS)"
"${compose[@]}" up -d --force-recreate zulip
cid="$("${compose[@]}" ps -q zulip)"
print -u2 "waiting for Zulip to become healthy (first boot runs puppet and all migrations)..."
for i in {1..90}; do
  health="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || print missing)"
  [[ "$health" == healthy ]] && break
  [[ "$health" == unhealthy || "$(docker inspect -f '{{.State.Status}}' "$cid")" == exited ]] && { print -u2 "zulip is $health"; exit 1; }
  sleep 10
done
[[ "$health" == healthy ]] || { print -u2 "zulip not healthy after 15 minutes"; exit 1; }
print -u2 "zulip healthy"

job() {  # like run_job: the last CHAT-RESULT line must say ok
  local out res
  out="$("${compose[@]}" run --rm -T mgmt chat:manage "$@" 2>&1)" || { print -u2 -r -- "$out" | tail -40; return 1; }
  res="$(print -r -- "$out" | sed -n 's/.*CHAT-RESULT: //p' | tail -n 1)"
  print -r -- "$res" | jq -e '.ok == true' >/dev/null || { print -u2 -r -- "$out" | tail -40; return 1; }
  print -r -- "$res"
}
# Assign first: errexit does not see a failed $(...) that is only an argument.
r1="$(job ensure-realm _root 'E2E Department' owner@e2e.test 'E2E Owner')"
print -u2 -r -- "mgmt: $r1"
jq -e '.created == true' <<<"$r1" >/dev/null || { print -u2 "expected the realm to be created"; exit 1; }
r2="$(job ensure-realm _root 'E2E Department' owner@e2e.test 'E2E Owner')"
print -u2 -r -- "mgmt: $r2 (rerun)"
jq -e '.created == false' <<<"$r2" >/dev/null || { print -u2 "rerun should find the realm, not create it"; exit 1; }
r3="$(job send-test-email e2e-check@e2e.test)"
print -u2 -r -- "mgmt: $r3"

# The -hc job: one healthy probe, and one against a path that fails (must ping /fail).
"${compose[@]}" run --rm -T hcjob
if "${compose[@]}" run --rm -T -e CHAT_HC_TARGET=http://zulip/no-such-health-endpoint hcjob; then
  print -u2 "the hc probe should have failed against a bad target"; exit 1
fi

"${compose[@]}" run --rm -T runner
print -u2 "e2e passed"
