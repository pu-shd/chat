#!/usr/bin/env zsh
# setup-github-repo.zsh — GitHub-side protections for a department's config repo
# (no Azure needed; run by a repo admin once the repo exists).
#
#   scripts/setup-github-repo.zsh --config <dept> [--admin-reviewer <login> ...] [--force] [--yes]
#
#   * main: protected (no force-push, no deletion) — required for the policy below. If
#     main is already protected, its rules are left exactly as they are (reviews or
#     status checks added since are kept); --force replaces them with these;
#   * Environment <env>: deployments only from protected branches, so a pushed feature
#     branch cannot obtain the Azure identity;
#   * Environment <env>-admin: the same, plus required reviewers who cannot approve
#     their own runs (prevent_self_review). Teardown and other destructive Operate
#     actions run in it (its own federated credential, setup-github-oidc.zsh), so a typed
#     phrase is never the only safeguard, and nobody can tear down alone. Reviewers are
#     each --admin-reviewer (repeatable; --reviewer is the same), default: you. Reviewers
#     already on the environment are kept — this adds, it never removes.
#     With a single reviewer, that person can start teardowns but nobody can approve
#     them: add a second reviewer.
source "${0:A:h}/common.zsh"
typeset -a REVIEWERS
FORCE=false
parse_common_args "$@"
set -- "${CHAT_ARGS_REST[@]}"
while (( $# )); do
  case "$1" in
    --admin-reviewer|--reviewer) REVIEWERS+=("${2:?$1 needs a GitHub login}"); shift 2 ;;
    --force) FORCE=true; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
CHAT_SKIP_RENDER_CHECK="${CHAT_SKIP_RENDER_CHECK:-}"
load_platform
require_cmd gh
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (gh auth login)"
REPO="$(jqp .github.repo)"
ENV="$(jqp .github.environment)"
(( ${#REVIEWERS} )) || REVIEWERS=("$(gh api user --jq .login)")
typeset -U REVIEWERS
typeset -a WANT_IDS
for login in "${REVIEWERS[@]}"; do
  [[ "$login" == [A-Za-z0-9-]## ]] || die "not a GitHub login: $login"
  id="$(gh api "users/$login" --jq .id)" || die "no GitHub user $login"
  [[ "$id" == <-> ]] || die "GitHub returned no id for $login"
  WANT_IDS+=("$id")
done
confirm "Protect main on $REPO and configure environments $ENV and $ENV-admin (reviewers: ${REVIEWERS[*]})?" || die "not changed"

# gh_get <api path> — the body on 200; return 1 on 404; any other failure ends the script
# (a transient error must not read as "not configured" and get it overwritten).
gh_get() {
  local errf out rc=0
  errf="$(mktemp)"
  out="$(gh api "$1" 2>"$errf")" || rc=$?
  if (( rc == 0 )); then rm -f "$errf"; print -r -- "$out"; return 0; fi
  if grep -q 'HTTP 404' "$errf"; then rm -f "$errf"; return 1; fi
  cat "$errf" >&2; rm -f "$errf"
  die "gh api $1 failed (exit $rc); nothing changed there" 3
}

if gh_get "repos/$REPO/branches/main/protection" >/dev/null && ! $FORCE; then
  log_ok "main already protected: rules left unchanged (--force replaces them with no force-push, no deletion)"
else
  gh api -X PUT "repos/$REPO/branches/main/protection" --input - >/dev/null <<JSON
{"required_status_checks": null, "enforce_admins": false, "required_pull_request_reviews": null,
 "restrictions": null, "allow_force_pushes": false, "allow_deletions": false}
JSON
  log_ok "main protected (no force-push, no deletion)"
  log_warn "main does not require pull request reviews or status checks: anyone with write access can push to it (and so deploy); add those rules in GitHub if you want them — reruns keep them"
fi

gh api -X PUT "repos/$REPO/environments/$ENV" --input - >/dev/null <<JSON
{"deployment_branch_policy": {"protected_branches": true, "custom_branch_policies": false}}
JSON
log_ok "environment $ENV: protected branches only"

# <env>-admin: merge our reviewers into any already there, keep its wait timer, and
# always forbid self-review so the person who starts a teardown cannot approve it.
CURRENT="$(gh_get "repos/$REPO/environments/$ENV-admin")" && rc=0 || rc=$?
case $rc in
  0) log_info "environment $ENV-admin exists: adding reviewers, keeping the ones it has" ;;
  1) CURRENT='{}' ;;
  *) exit $rc ;;  # gh_get explained it (inside $(...) its die only ends the subshell)
esac
BODY="$(print -r -- "$CURRENT" | jq -c --argjson want "[${(j:,:)WANT_IDS}]" '
  ([.protection_rules[]? | select(.type == "required_reviewers") | .reviewers[]?
     | {type, id: .reviewer.id}] + ($want | map({type: "User", id: .}))) as $all
  | ($all | unique_by([.type, .id])) as $reviewers
  | {deployment_branch_policy: {protected_branches: true, custom_branch_policies: false},
     reviewers: $reviewers, prevent_self_review: true}
  + ([.protection_rules[]? | select(.type == "wait_timer") | .wait_timer] | if length > 0 then {wait_timer: .[0]} else {} end)')"
COUNT="$(print -r -- "$BODY" | jq '.reviewers | length')"
(( COUNT <= 6 )) || die "environment $ENV-admin would have $COUNT reviewers; GitHub allows 6 — remove some in its settings first"
print -r -- "$BODY" | gh api -X PUT "repos/$REPO/environments/$ENV-admin" --input - >/dev/null
log_ok "environment $ENV-admin: protected branches only, $COUNT reviewer(s), no self-review"
if [[ "$(print -r -- "$BODY" | jq -r '.reviewers | map(.type) | join(",")')" == User ]]; then
  log_warn "environment $ENV-admin has ONE reviewer: they can start teardowns but cannot approve their own, so no teardown can be approved until a second reviewer is added (--admin-reviewer <login>)"
fi
