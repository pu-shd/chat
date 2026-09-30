#!/usr/bin/env zsh
# setup-github-repo.zsh — GitHub-side protections for a department's config repo
# (no Azure needed; run by a repo admin once the repo exists).
#
#   scripts/setup-github-repo.zsh --config <dept> [--reviewer <login>] [--yes]
#
#   * main: protected (no force-push, no deletion) — required for the policy below;
#   * Environment <env>: deployments only from protected branches, so a pushed feature
#     branch cannot obtain the Azure identity;
#   * Environment <env>-admin: the same, plus a required reviewer. Teardown and other
#     destructive Operate actions run in it (its own federated credential,
#     setup-github-oidc.zsh), so a typed phrase is never the only safeguard.
source "${0:A:h}/common.zsh"
REVIEWER=""
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --reviewer) REVIEWER="${2:?}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
CHAT_SKIP_RENDER_CHECK="${CHAT_SKIP_RENDER_CHECK:-}"
load_platform
require_cmd gh
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (gh auth login)"
REPO="$(jqp .github.repo)"
ENV="$(jqp .github.environment)"
REVIEWER="${REVIEWER:-$(gh api user --jq .login)}"
REVIEWER_ID="$(gh api "users/$REVIEWER" --jq .id)"
confirm "Protect main on $REPO and configure environments $ENV and $ENV-admin (reviewer: $REVIEWER)?" || die "not changed"

gh api -X PUT "repos/$REPO/branches/main/protection" --input - >/dev/null <<JSON
{"required_status_checks": null, "enforce_admins": false, "required_pull_request_reviews": null,
 "restrictions": null, "allow_force_pushes": false, "allow_deletions": false}
JSON
log_ok "main protected (no force-push, no deletion)"

gh api -X PUT "repos/$REPO/environments/$ENV" --input - >/dev/null <<JSON
{"deployment_branch_policy": {"protected_branches": true, "custom_branch_policies": false}}
JSON
log_ok "environment $ENV: protected branches only"

gh api -X PUT "repos/$REPO/environments/$ENV-admin" --input - >/dev/null <<JSON
{"deployment_branch_policy": {"protected_branches": true, "custom_branch_policies": false},
 "reviewers": [{"type": "User", "id": $REVIEWER_ID}], "prevent_self_review": false}
JSON
log_ok "environment $ENV-admin: protected branches only, reviewer $REVIEWER"
