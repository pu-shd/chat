#!/usr/bin/env zsh
# update-server.zsh — day-2 operations on one server that are not a config change.
#
#   scripts/update-server.zsh --config <dept> --server <name> --image <ref@sha256:...>
#       Upgrade to a new image (same maintenance sequence as deploy-server.zsh).
#   scripts/update-server.zsh --config <dept> --server <name> --restart
#       Restart the active revision (e.g. after rotating a Key Vault secret; Container
#       Apps re-reads versionless Key Vault references on restart).
source "${0:A:h}/common.zsh"
parse_common_args "$@"
if [[ "${CHAT_ARGS_REST[1]:-}" == --restart ]]; then
  load_platform
  load_server
  az_login
  rev="$(az containerapp revision list -g "$RG" -n "$APP_NAME" --query '[?properties.active].name | [0]' -o tsv)"
  [[ -n "$rev" ]] || die "$APP_NAME has no active revision"
  az_retry containerapp revision restart -g "$RG" -n "$APP_NAME" --revision "$rev" --output none
  log_ok "restarted $rev"
  exit 0
fi
exec "${0:A:h}/deploy-server.zsh" "$@"
