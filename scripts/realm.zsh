#!/usr/bin/env zsh
# realm.zsh — Zulip organizations (realms) on one server, through its -mgmt job.
#
#   scripts/realm.zsh --config <dept> --server <name> --list
#   scripts/realm.zsh --config <dept> --server <name> --ensure <slug|_root>   (from chat.yml)
#   scripts/realm.zsh --config <dept> --server <name> --deactivate <slug>     (type-to-confirm)
#   scripts/realm.zsh --config <dept> --server <name> --set-role <slug|_root> <email> <role> [full-name]
#       (owner|admin|moderator|member|guest; type-to-confirm for owner/admin. Hands a realm
#       over or recovers it when its owner has left; creates the account when a full name
#       is given, and the person then signs in with Entra.)
#   scripts/realm.zsh --config <dept> --server <name> --register-push
#   scripts/realm.zsh --config <dept> --server <name> --send-test-email <address>
#
# Realms are never deleted here: deactivation keeps every message and can be undone
# with manage.py reactivate_realm.
source "${0:A:h}/common.zsh"
ACTION="" SLUG="" R_EMAIL="" R_ROLE="" R_NAME=""
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --list) ACTION=list; shift ;;
    --ensure) ACTION=ensure; SLUG="${2:?}"; shift 2 ;;
    --deactivate) ACTION=deactivate; SLUG="${2:?}"; shift 2 ;;
    --register-push) ACTION=push; shift ;;
    --set-role) ACTION=role; SLUG="${2:?}"; R_EMAIL="${3:?}"; R_ROLE="${4:?}"; R_NAME=""
      shift 4
      if (( $# )) && [[ "$1" != --* ]]; then R_NAME="$1"; shift; fi ;;
    --send-test-email) ACTION=mail; SLUG="${2:?an email address}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$ACTION" ]] || die "one of --list, --ensure, --deactivate, --set-role, --register-push, --send-test-email"
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
  role)
    [[ "$R_EMAIL" =~ '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$' ]] || die "not an email address: $R_EMAIL"
    [[ "$R_ROLE" =~ '^(owner|admin|moderator|member|guest)$' ]] || die "role must be owner, admin, moderator, member or guest"
    if [[ "$R_ROLE" == owner || "$R_ROLE" == admin ]]; then confirm_typed "GRANT $R_ROLE $R_EMAIL"; fi
    args=(chat:manage set-role "$SLUG" "$R_EMAIL" "$R_ROLE")
    [[ -n "$R_NAME" ]] && args+=("$R_NAME")
    RUN_JOB_EXPECT=".email == \"$R_EMAIL\" and .role == \"$R_ROLE\"" run_job "$MGMT_JOB" mgmt "${args[@]}"
    ;;
esac
