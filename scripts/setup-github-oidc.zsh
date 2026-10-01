#!/usr/bin/env zsh
# setup-github-oidc.zsh — secret-free GitHub Actions → Azure login for a config repo
# (after meet's infrastructure/setup-github-oidc.sh).
#
#   scripts/setup-github-oidc.zsh --config <dept-dir> [--set-gh-vars [--admin-reviewer <login> ...]] [--yes]
#
# Creates (or reuses) the app registration <prefix>-github-actions with one federated
# credential whose subject is the config repo's GitHub Environment:
#     repo:<github.repo>:environment:<github.environment>
# and grants it Contributor on the resource group plus Key Vault Secrets Officer on the
# vault. It gets no Entra (Graph) rights: app registrations for Zulip sign-in are
# managed by an operator with entra-app.zsh, never by CI.
#
# --set-gh-vars also runs setup-github-repo.zsh (protected main; environments <env> and
# <env>-admin, the OIDC subjects; --admin-reviewer is passed on: who may approve
# teardowns, never their own) and sets
# AZURE_CLIENT_ID, AZURE_TENANT_ID and AZURE_SUBSCRIPTION_ID as repository variables:
# the config repo's deploy job passes them to pu-shd/chat's reusable workflow, and a job
# that calls a reusable workflow cannot read environment-scoped variables.
source "${0:A:h}/common.zsh"
SET_GH_VARS=false
typeset -a REPO_ARGS
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --set-gh-vars) SET_GH_VARS=true; shift ;;
    --admin-reviewer) REPO_ARGS+=(--admin-reviewer "${2:?--admin-reviewer needs a GitHub login}"); shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
(( ${#REPO_ARGS} == 0 )) || $SET_GH_VARS || die "--admin-reviewer only applies with --set-gh-vars"
load_platform
az_login

REPO="$(jqp .github.repo)"
GH_ENV="$(jqp .github.environment)"
APP_DISPLAY="$(jqp .names.github_app)"
SUBJECT="repo:${REPO}:environment:${GH_ENV}"  # braces: $REPO:e would be a zsh modifier
CRED_NAME="gh-${GH_ENV}"
ADMIN_SUBJECT="repo:${REPO}:environment:${GH_ENV}-admin"

az group show -n "$RG" >/dev/null 2>&1 || die "$RG does not exist; run deploy-platform.zsh first"

log_step "App registration $APP_DISPLAY"
# Never adopt a look-alike: it would be granted Contributor and Key Vault Secrets Officer.
APP_ID="$(entra_app_by_name "$APP_DISPLAY")" || die "not granting anything to $APP_DISPLAY"
if [[ -n "$APP_ID" ]]; then
  log_ok "exists ($APP_ID)"
else
  APP_ID="$(az ad app create --display-name "$APP_DISPLAY" --sign-in-audience AzureADMyOrg --query appId -o tsv)"
  log_ok "created ($APP_ID)"
fi
az ad sp show --id "$APP_ID" >/dev/null 2>&1 || az ad sp create --id "$APP_ID" --output none
SP_ID="$(az ad sp show --id "$APP_ID" --query id -o tsv)"

log_step "Federated credential $CRED_NAME → $SUBJECT"
EXISTING="$(az ad app federated-credential list --id "$APP_ID" --query "[?name=='$CRED_NAME'].subject" -o tsv)"
if [[ "$EXISTING" == "$SUBJECT" ]]; then
  log_ok "present"
else
  [[ -n "$EXISTING" ]] && az ad app federated-credential delete --id "$APP_ID" --federated-credential-id "$CRED_NAME"
  CRED="$(mktemp)"
  jq -n --arg n "$CRED_NAME" --arg s "$SUBJECT" \
    '{name: $n, issuer: "https://token.actions.githubusercontent.com", subject: $s, audiences: ["api://AzureADTokenExchange"]}' > "$CRED"
  az ad app federated-credential create --id "$APP_ID" --parameters "@$CRED" --output none
  rm -f "$CRED"
  log_ok "created"
fi

# Destructive workflows (Teardown, realm deactivation) run in <env>-admin, which needs a
# reviewer's approval (setup-github-repo.zsh); it signs in through its own credential.
ADMIN_CRED="gh-${GH_ENV}-admin"
if [[ "$(az ad app federated-credential list --id "$APP_ID" --query "[?name=='$ADMIN_CRED'].subject" -o tsv)" != "$ADMIN_SUBJECT" ]]; then
  CRED="$(mktemp)"
  jq -n --arg n "$ADMIN_CRED" --arg s "$ADMIN_SUBJECT" \
    '{name: $n, issuer: "https://token.actions.githubusercontent.com", subject: $s, audiences: ["api://AzureADTokenExchange"]}' > "$CRED"
  az ad app federated-credential create --id "$APP_ID" --parameters "@$CRED" --output none
  rm -f "$CRED"
  log_ok "federated credential $ADMIN_CRED → $ADMIN_SUBJECT"
fi

grant() {  # role scope
  if [[ -n "$(az role assignment list --assignee "$SP_ID" --scope "$2" --role "$1" --query '[0].id' -o tsv)" ]]; then
    log_ok "$1 already on $(basename "$2")"
  else
    az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
      --role "$1" --scope "$2" --output none
    log_ok "granted $1 on $(basename "$2")"
  fi
}
log_step "Role assignments"
grant Contributor "$(az group show -n "$RG" --query id -o tsv)"
grant "Key Vault Secrets Officer" "$(az keyvault show -n "$KV_NAME" -g "$RG" --query id -o tsv)"

print -r -- ""
print -r -- "$REPO needs a GitHub Environment '$GH_ENV' and these repository variables (not secrets):"
print -r -- "  AZURE_CLIENT_ID=$APP_ID"
print -r -- "  AZURE_TENANT_ID=$TENANT_ID"
print -r -- "  AZURE_SUBSCRIPTION_ID=$SUB_ID"
print -r -- "Optional secret: PUGWIPS_READ_TOKEN (contents:read on PrincetonUniversity/pugwips) for the live IP gate."

if $SET_GH_VARS; then
  require_cmd gh
  gh auth status >/dev/null 2>&1 || die "gh is not authenticated (gh auth login)"
  confirm "Protect $REPO, create environments '$GH_ENV' and '$GH_ENV-admin', and set those three repository variables?" || die "not changed"
  "${0:A:h}/setup-github-repo.zsh" --config "$CONFIG_DIR" --yes "${REPO_ARGS[@]}"
  gh variable set AZURE_CLIENT_ID --repo "$REPO" --body "$APP_ID"
  gh variable set AZURE_TENANT_ID --repo "$REPO" --body "$TENANT_ID"
  gh variable set AZURE_SUBSCRIPTION_ID --repo "$REPO" --body "$SUB_ID"
  log_ok "GitHub environment $GH_ENV configured"
fi
