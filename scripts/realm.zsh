#!/usr/bin/env zsh
# realm.zsh — Zulip organizations (realms) on one server, through its -mgmt job.
#
#   scripts/realm.zsh --config <dept> --server <name> --list
#   scripts/realm.zsh --config <dept> --server <name> --ensure <slug|_root>   (from chat.yml)
#   scripts/realm.zsh --config <dept> --server <name> --deactivate <slug>     (type-to-confirm)
#   scripts/realm.zsh --config <dept> --server <name> --register-push
#   scripts/realm.zsh --config <dept> --server <name> --send-test-email <address>
#
# Realms are never deleted here: deactivation keeps every message and can be undone
# with manage.py reactivate_realm.
source "${0:A:h}/common.zsh"
ACTION="" SLUG=""
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --list) ACTION=list; shift ;;
    --ensure) ACTION=ensure; SLUG="${2:?}"; shift 2 ;;
    --deactivate) ACTION=deactivate; SLUG="${2:?}"; shift 2 ;;
    --register-push) ACTION=push; shift ;;
    --send-test-email) ACTION=mail; SLUG="${2:?an email address}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$ACTION" ]] || die "one of --list, --ensure, --deactivate, --register-push, --send-test-email"
load_platform
load_server
az_login

case "$ACTION" in
  list)
    run_job "$MGMT_JOB" mgmt chat:manage list-realms | jq -r '.realms[] | "\(.slug // "" | if . == "" then "(root)" else . end)\t\(.name)\t\(if .deactivated then "DEACTIVATED" else "active" end)\t\(.url)"'
    ;;
  ensure)
    key="${SLUG/#_root/}"
    row="$(jq -c --arg s "$key" '.realms[] | select(.slug == $s)' "$SERVER_JSON")"
    [[ -n "$row" ]] || die "realm '$SLUG' is not in chat.yml for $SERVER"
    run_job "$MGMT_JOB" mgmt chat:manage ensure-realm "${key:-_root}" "$(jq -r .name <<<"$row")" \
      "$(jq -r .owner.email <<<"$row")" "$(jq -r .owner.name <<<"$row")"
    ;;
  deactivate)
    [[ -n "$SLUG" && "$SLUG" != _root ]] || die "refusing to deactivate a server's root realm; tear the server down instead"
    confirm_typed "DEACTIVATE $SERVER/$SLUG"
    run_job "$MGMT_JOB" mgmt chat:manage deactivate-realm "$SLUG"
    ;;
  push)
    run_job "$MGMT_JOB" mgmt chat:manage register-push
    ;;
  mail)
    run_job "$MGMT_JOB" mgmt chat:manage send-test-email "$SLUG"
    ;;
esac
