"""Regression tests for the audit fixes in scripts/*.zsh — each runs a script for real
against the recording stand-ins (an unmatched call fails with exit 97, so nothing here
can pass by a command being silently skipped)."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import ME, NOT_FOUND, az_basics, entra_app
from test_ops_scripts import API_KEY, PING_KEY, acs_dept, acs_rules, grant_rules, keepalive_rules, role_rules, V
from test_scripts import DOMAIN, IMAGE, deploy, deploy_rules, index_of

TRANSIENT = {"exit": 1, "stderr": "ERROR: ('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))"}
DENIED = {"exit": 1, "stderr": "ERROR: (AuthorizationFailed) The client 'x' does not have authorization to perform action"}
SKIP_RENDER = {"CHAT_SKIP_RENDER_CHECK": "1"}


def edit_json(path: Path, fn) -> None:
    d = json.loads(path.read_text())
    fn(d)
    path.write_text(json.dumps(d))


def with_hc_identity(dept, server="dept"):
    """identities.hc as render.py writes it when healthchecks are enabled."""
    edit_json(dept.path / "generated" / "servers" / f"{server}.json", lambda d: d["identities"].update(
        hc={"name": f"orfe-chat-{server}-hc-id", "secrets": ["healthchecks-ping-key"]}))


# ---------------------------------------------------------------- 1. Entra apps by display name

def oidc_rules(sh, app_id="66666666-6666-6666-6666-666666666666"):
    az_basics(sh)
    sh.on("az", r"^group show -n orfe-chat-rg$", "{}")
    sh.on("az", r"^group show -n orfe-chat-rg --query id", "/subs/x/rg")
    sh.on("az", r"^keyvault show -n orfe-chat-kv -g orfe-chat-rg --query id", "/subs/x/vaults/orfe-chat-kv")
    sh.on("az", r"^ad sp show --id \S+$", "{}")
    sh.on("az", r"^ad sp show --id \S+ --query id", "sp-gh")
    sh.on("az", r"^ad app federated-credential list .*gh-orfe-admin'", "repo:pu-orfe/chat-config:environment:orfe-admin")
    sh.on("az", r"^ad app federated-credential list .*gh-orfe'", "repo:pu-orfe/chat-config:environment:orfe")
    sh.on("az", r"^role assignment list ", "/ra/1")
    return sh


@pytest.mark.parametrize("listing, owners, message", [
    ("aaaa-1\naaaa-2", None, "2 app registrations are named orfe-chat-github-actions (aaaa-1, aaaa-2)"),
    ("aaaa-1", ["someone-else"], "is not owned by you"),
])
def test_github_oidc_never_adopts_a_look_alike_app(run, dept, shims, listing, owners, message):
    shims.on("az", r"^ad app list --display-name orfe-chat-github-actions ", listing)
    if owners is not None:
        shims.on("az", r"^ad app owner list --id aaaa-1 ", "\n".join(owners))
    oidc_rules(shims)
    r = run("setup-github-oidc.zsh", "--config", str(dept.path))
    assert r.returncode != 0 and message in r.stderr, r.stderr
    assert "not granting anything to orfe-chat-github-actions" in r.stderr
    assert not any(c.startswith(("role assignment create", "ad app federated-credential", "ad app create"))
                   for c in shims.joined("az"))


def test_github_oidc_uses_the_app_it_owns(run, dept, shims):
    entra_app(shims, "orfe-chat-github-actions", "66666666-6666-6666-6666-666666666666", owners=("other", ME))
    oidc_rules(shims)
    r = run("setup-github-oidc.zsh", "--config", str(dept.path))
    assert r.returncode == 0, r.stderr
    assert "AZURE_CLIENT_ID=66666666-6666-6666-6666-666666666666" in r.stdout


def test_github_oidc_federated_subjects_are_the_repo_environments(run, dept, shims):
    """Regression: "repo:$REPO:environment" applied zsh's :e modifier to $REPO."""
    shims.on("az", r"^ad app federated-credential list ", "")
    shims.on("az", r"^ad app federated-credential create ")
    entra_app(shims, "orfe-chat-github-actions", "66666666-6666-6666-6666-666666666666")
    oidc_rules(shims)
    r = run("setup-github-oidc.zsh", "--config", str(dept.path))
    assert r.returncode == 0, r.stderr
    made = [json.loads(next(iter(c["at_files"].values()))) for c in shims.calls("az")
            if c["args"][:3] == ["ad", "app", "federated-credential"] and c["args"][3] == "create"]
    assert [(m["name"], m["subject"]) for m in made] == [
        ("gh-orfe", "repo:pu-orfe/chat-config:environment:orfe"),
        ("gh-orfe-admin", "repo:pu-orfe/chat-config:environment:orfe-admin")]


def test_entra_app_refuses_a_foreign_app_before_touching_it(run, dept, shims):
    az_basics(shims)
    entra_app(shims, "orfe-chat-groups-zulip", "77777777-7777-7777-7777-777777777777", owners=("intruder",))
    r = run("entra-app.zsh", "--config", str(dept.path), "--server", "groups")
    assert r.returncode != 0 and "is not owned by you" in r.stderr
    assert "az ad app owner add --id 77777777-7777-7777-7777-777777777777" in r.stderr  # how to fix it
    assert not any(c.startswith(("ad app update", "ad app credential", "ad sp", "keyvault secret set"))
                   for c in shims.joined("az"))


def test_ownership_is_checked_for_a_service_principal_login(run, dept, shims):
    """CI / a service principal: the owner must be the SP of the logged-in client id."""
    shims.on("az", r"^account show --query user\.type", "servicePrincipal")
    shims.on("az", r"^account show --query user\.name", "client-id-1")
    shims.on("az", r"^ad sp show --id client-id-1 --query id", "sp-oid-1")
    az_basics(shims)
    entra_app(shims, "orfe-chat-dept-zulip", "99999999-9999-9999-9999-999999999999", owners=("sp-oid-1",))
    r = run("entra-app.zsh", "--config", str(dept.path), "--server", "dept")
    # Ownership passed (the SP owns it); the pinned-client-id check still applies after it.
    assert r.returncode != 0 and "pins entra.client_id=11111111" in r.stderr
    assert "not owned" not in r.stderr
    assert not any("signed-in-user" in c for c in shims.joined("az"))


def test_acs_refuses_an_impostor_smtp_app(run, dept, shims):
    acs_dept(dept)
    shims.on("az", r"^ad app list --display-name orfe-chat-acs-smtp ", "55555555-5555-5555-5555-555555555555\n88888888-8888-8888-8888-888888888888")
    acs_rules(shims, states=V)
    r = run("acs-email.zsh", "--config", str(dept.path))
    assert r.returncode != 0 and "2 app registrations are named orfe-chat-acs-smtp" in r.stderr
    assert not any(c.startswith(("role assignment create", "ad app credential", "communication smtp-username create"))
                   for c in shims.joined("az"))


def test_teardown_entra_never_deletes_another_teams_app(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show ", **NOT_FOUND)
    shims.on("az", r"^containerapp job show ", **NOT_FOUND)
    entra_app(shims, "orfe-chat-lab-zulip", "44444444-4444-4444-4444-444444444444", owners=("another-team",))
    shims.on("az", r"^ad app list ", "")
    r = run("teardown.zsh", "--config", str(dept.path), "--server", "lab", "--entra",
            env={"CHAT_CONFIRM": "TEARDOWN orfe lab"})
    assert r.returncode != 0 and "not deleting app registration orfe-chat-lab-zulip" in r.stderr
    assert not any(c.startswith("ad app delete") for c in shims.joined("az"))


def test_teardown_server_prints_no_delete_command_for_an_ambiguous_app(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", **NOT_FOUND)
    shims.on("az", r"^containerapp job show ", **NOT_FOUND)
    shims.on("az", r"^ad app list --display-name orfe-chat-dept-zulip ", "a-1\na-2")
    r = run("teardown-server.zsh", "--config", str(dept.path), "--server", "dept",
            env={"CHAT_CONFIRM": "CONFIRM-DELETE dept"})
    assert r.returncode == 0, r.stderr
    assert "To remove it: az ad app delete" not in r.stderr and "not printing a delete command" in r.stderr


# ---------------------------------------------------------------- 2. app_exists / az_exists

@pytest.mark.parametrize("failure", [TRANSIENT, DENIED])
def test_an_az_error_is_never_a_first_deploy(run, dept, shims, failure):
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", **failure)
    deploy_rules(shims)
    r = deploy(run, dept, env={"AZ_RETRY_MAX": "2"})
    assert r.returncode != 0 and "refusing to guess" in r.stderr
    assert "first deploy" not in r.stderr
    assert not any(c.startswith("deployment ") for c in shims.joined("az"))
    shows = [c for c in shims.joined("az") if c.startswith("containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id")]
    assert len(shows) == (2 if failure is TRANSIENT else 1)  # transient: retried first


def test_an_az_error_never_regenerates_a_secret(run, dept, shims):
    shims.on("az", r"^keyvault secret show --vault-name orfe-chat-kv --name dept-redis-password --query id", **DENIED)
    grant_rules(shims)
    r = run("grant-access.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode != 0 and "refusing to guess" in r.stderr
    assert not any(c.startswith("keyvault secret set") for c in shims.joined("az"))


def test_teardown_stops_if_it_cannot_read_the_bindings(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", "/app")
    shims.on("az", r"^containerapp show .*customDomains", **DENIED)
    r = run("teardown-server.zsh", "--config", str(dept.path), "--server", "dept",
            env={"CHAT_CONFIRM": "CONFIRM-DELETE dept"})
    assert r.returncode != 0 and "could not read orfe-chat-dept's hostname bindings; nothing was deleted" in r.stderr
    assert not any("delete" in c for c in shims.joined("az"))


# ---------------------------------------------------------------- 3. deploy-platform linked domains

def platform_rules(sh, *, acs_exists=True, linked=None):
    az_basics(sh)
    sh.on("az", r"^group show -n orfe-chat-rg --query id", "/subs/x/rg")
    sh.on("az", r"^keyvault show -n orfe-chat-kv -g orfe-chat-rg --query id", "/subs/x/vaults/orfe-chat-kv")
    sh.on("az", r"^role assignment list ", "/ra/1")
    sh.on("az", r"^keyvault secret list ")
    sh.on("az", r"^keyvault secret show-deleted ", **NOT_FOUND)
    sh.on("az", r"^keyvault secret show .* --query id", "id")
    sh.on("az", r"^keyvault secret show .*pg-admin-password --query value", "pg-pass")
    if acs_exists:
        sh.on("az", r"^communication show -g orfe-chat-rg -n orfe-chat-acs --query id", "/subs/x/acs")
        if linked is not None:
            sh.on("az", r"^communication show .*linkedDomains", linked)
    else:
        sh.on("az", r"^communication show ", **NOT_FOUND)
    sh.on("az", r"^deployment group create ", {"defaultDomain": {"value": DOMAIN}, "customDomainVerificationId": {"value": "V"},
                                              "staticIp": {"value": "1.2.3.4"}, "registryLoginServer": {"value": "r"}})


def platform_params(shims):
    call = next(c for c in shims.calls("az") if c["args"][:3] == ["deployment", "group", "create"])
    return json.loads(next(iter(call["at_files"].values())))["parameters"]


def test_platform_keeps_linked_domains(run, dept, shims):
    acs_dept(dept)
    platform_rules(shims, linked=["/subs/x/email/domains/orfe.example.edu"])
    r = run("deploy-platform.zsh", "--config", str(dept.path))
    assert r.returncode == 0, r.stderr
    assert platform_params(shims)["acsLinkedDomains"]["value"] == ["/subs/x/email/domains/orfe.example.edu"]


def test_platform_refuses_to_unlink_on_a_read_error(run, dept, shims):
    acs_dept(dept)
    shims.on("az", r"^communication show .*linkedDomains", **DENIED)
    platform_rules(shims)
    r = run("deploy-platform.zsh", "--config", str(dept.path))
    assert r.returncode != 0 and "could not read orfe-chat-acs's linked email domains" in r.stderr
    assert not any(c.startswith("deployment group create") for c in shims.joined("az"))


def test_platform_first_acs_deploy_links_nothing(run, dept, shims):
    acs_dept(dept)
    platform_rules(shims, acs_exists=False)
    r = run("deploy-platform.zsh", "--config", str(dept.path))
    assert r.returncode == 0, r.stderr
    assert platform_params(shims)["acsLinkedDomains"]["value"] == []


# ---------------------------------------------------------------- 4. maintenance window

def test_failed_revision_list_stops_before_the_new_image_runs(run, dept, shims):
    shims.on("az", r"^containerapp revision list ", **DENIED)
    deploy_rules(shims)
    r = deploy(run, dept)
    assert r.returncode != 0 and "could not list orfe-chat-dept's active revisions" in r.stderr
    assert not any("-n chat-dept-app" in c for c in shims.joined("az"))


def test_every_active_revision_is_stopped(run, dept, shims):
    shims.on("az", r"^containerapp revision list .*properties\.active", "orfe-chat-dept--r1\norfe-chat-dept--r0")
    deploy_rules(shims)
    r = deploy(run, dept)
    assert r.returncode == 0, r.stderr
    stopped = [c.split("--revision ")[1].split()[0] for c in shims.joined("az") if "revision deactivate" in c]
    assert stopped == ["orfe-chat-dept--r1", "orfe-chat-dept--r0"]


# ---------------------------------------------------------------- health wait matches the ~21 min startup probe

def test_unhealthy_is_tolerated_for_the_startup_window(run, dept, shims):
    shims.on("az", r"^containerapp revision show ", "Unhealthy\tRunning", times=65)
    deploy_rules(shims)
    r = deploy(run, dept, env={"HEALTH_POLL": "0"})
    assert r.returncode == 0, r.stderr
    assert len([c for c in shims.joined("az") if c.startswith("containerapp revision show")]) == 66


def test_sustained_unhealthy_fails_after_66_polls(run, dept, shims):
    shims.on("az", r"^containerapp revision show ", "Unhealthy\tRunning")
    shims.on("az", r"^containerapp logs show ", "boom")
    deploy_rules(shims)
    r = deploy(run, dept, env={"HEALTH_POLL": "0"})
    assert r.returncode != 0 and "is Unhealthy" in r.stderr
    assert len([c for c in shims.joined("az") if c.startswith("containerapp revision show")]) == 66


def test_health_defaults_cover_the_startup_probe():
    from conftest import ROOT
    text = (ROOT / "scripts" / "deploy-server.zsh").read_text()
    assert "${HEALTH_TIMEOUT:-1500}" in text and "${UNHEALTHY_LIMIT:-66}" in text


# ---------------------------------------------------------------- 5. healthchecks --delete by exact slug

def test_delete_server_checks_never_takes_a_longer_named_servers(run, dept, shims):
    dept.edit(lambda c: c.update(healthchecks={"enabled": True}))
    dept.render()
    # A server "lab-b" would own orfe-chat-lab-b-*: those match the old prefix orfe-chat-lab-.
    edit_json(dept.path / "generated" / "platform.json", lambda d: d["healthchecks"]["checks"].append(
        {**d["healthchecks"]["checks"][0], "slug": "orfe-chat-lab-b-health"}))
    for slug in ("health", "web", "ip-gate"):
        shims.on("curl", rf"GET .*slug=orfe-chat-lab-{slug}$",
                 {"checks": [{"uuid": f"u-{slug}", "update_url": f"https://healthchecks.io/api/v3/checks/u-{slug}"}]})
    shims.on("curl", r"-X DELETE ", {})
    r = run("healthchecks.zsh", "--config", str(dept.path), "--delete", "--server", "lab",
            env={"HEALTHCHECKS_API_KEY": API_KEY, "CHAT_CONFIRM": "DELETE-CHECKS lab", **SKIP_RENDER})
    assert r.returncode == 0, r.stderr
    gets = [c["args"][-1] for c in shims.calls("curl") if "GET" in c["args"]]
    assert not any("lab-b" in g for g in gets)
    deletes = sorted(c["args"][-1].rsplit("/", 1)[1] for c in shims.calls("curl") if "DELETE" in c["args"])
    assert deletes == ["u-health", "u-ip-gate", "u-web"]


# ---------------------------------------------------------------- 6. teardown-platform

@pytest.mark.parametrize("failing", ["containerapp list", "containerapp job list"])
def test_teardown_platform_refuses_when_a_list_fails(run, dept, shims, failing):
    az_basics(shims)
    shims.on("az", r"^group show -n orfe-chat-rg --query id", "/rg")
    shims.on("az", rf"^{failing} ", **DENIED)
    shims.on("az", r"^containerapp (job )?list ", "")
    r = run("teardown-platform.zsh", "--config", str(dept.path), env={"CHAT_CONFIRM": "DELETE-PLATFORM orfe-chat-rg"})
    assert r.returncode != 0 and "could not list" in r.stderr
    assert not any(c.startswith("group delete") for c in shims.joined("az"))


def test_teardown_platform_names_what_is_left(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^group show -n orfe-chat-rg --query id", "/rg")
    shims.on("az", r"^containerapp list ", "")
    shims.on("az", r"^containerapp job list ", "orfe-chat-dept-dbinit")
    r = run("teardown-platform.zsh", "--config", str(dept.path), env={"CHAT_CONFIRM": "DELETE-PLATFORM orfe-chat-rg"})
    assert r.returncode != 0 and "servers remain in orfe-chat-rg" in r.stderr and "orfe-chat-dept-dbinit" in r.stderr
    assert not any(c.startswith("group delete") for c in shims.joined("az"))


# ---------------------------------------------------------------- 7. setup-github-repo

GH_404 = {"exit": 1, "stderr": "gh: Not Found (HTTP 404)"}


def gh_rules(sh, *, protected=False, admin_env=None, users=(("me-login", 1001),)):
    sh.on("gh", r"^auth status")
    sh.on("gh", r"^api user --jq \.login$", users[0][0])
    for login, uid in users:
        sh.on("gh", rf"^api users/{login} --jq \.id$", str(uid))
    if protected:
        sh.on("gh", r"^api repos/pu-orfe/chat-config/branches/main/protection$", {"required_pull_request_reviews": {}})
    else:
        sh.on("gh", r"^api repos/pu-orfe/chat-config/branches/main/protection$", **GH_404)
    if admin_env is None:
        sh.on("gh", r"^api repos/pu-orfe/chat-config/environments/orfe-admin$", **GH_404)
    else:
        sh.on("gh", r"^api repos/pu-orfe/chat-config/environments/orfe-admin$", admin_env)
    sh.on("gh", r"^api -X PUT ")
    return sh


def puts(shims):
    return {c["args"][3]: json.loads(c["stdin"]) for c in shims.calls("gh") if c["args"][:3] == ["api", "-X", "PUT"]}


def test_github_repo_fresh_setup_forbids_self_review_and_warns_on_one_reviewer(run, dept, shims):
    gh_rules(shims)
    r = run("setup-github-repo.zsh", "--config", str(dept.path), "--yes")
    assert r.returncode == 0, r.stderr
    p = puts(shims)
    assert p["repos/pu-orfe/chat-config/branches/main/protection"]["required_pull_request_reviews"] is None
    admin = p["repos/pu-orfe/chat-config/environments/orfe-admin"]
    assert admin["prevent_self_review"] is True
    assert admin["reviewers"] == [{"type": "User", "id": 1001}]
    assert admin["deployment_branch_policy"] == {"protected_branches": True, "custom_branch_policies": False}
    assert "ONE reviewer" in r.stderr and "cannot approve their own" in r.stderr
    assert "does not require pull request reviews" in r.stderr


def test_github_repo_keeps_existing_protection_and_merges_reviewers(run, dept, shims):
    existing = {"name": "orfe-admin", "protection_rules": [
        {"type": "wait_timer", "wait_timer": 5},
        {"type": "required_reviewers", "prevent_self_review": False, "reviewers": [
            {"type": "User", "reviewer": {"id": 11, "login": "old"}}, {"type": "Team", "reviewer": {"id": 22}},
            {"type": "User", "reviewer": {"id": 2002, "login": "bob"}}]}]}
    gh_rules(shims, protected=True, admin_env=existing, users=(("alice", 2001), ("bob", 2002)))
    r = run("setup-github-repo.zsh", "--config", str(dept.path), "--admin-reviewer", "alice", "--admin-reviewer", "bob", "--yes")
    assert r.returncode == 0, r.stderr
    p = puts(shims)
    assert "repos/pu-orfe/chat-config/branches/main/protection" not in p  # left exactly as it was
    assert "main already protected: rules left unchanged" in r.stderr
    admin = p["repos/pu-orfe/chat-config/environments/orfe-admin"]
    assert sorted((x["type"], x["id"]) for x in admin["reviewers"]) == [
        ("Team", 22), ("User", 11), ("User", 2001), ("User", 2002)]
    assert admin["prevent_self_review"] is True and admin["wait_timer"] == 5
    assert "ONE reviewer" not in r.stderr


def test_github_repo_force_replaces_main_protection(run, dept, shims):
    gh_rules(shims, protected=True)
    r = run("setup-github-repo.zsh", "--config", str(dept.path), "--force", "--yes")
    assert r.returncode == 0, r.stderr
    assert "repos/pu-orfe/chat-config/branches/main/protection" in puts(shims)


def test_github_repo_error_reading_protection_changes_nothing(run, dept, shims):
    shims.on("gh", r"^api repos/pu-orfe/chat-config/branches/main/protection$", exit=1, stderr="gh: Server Error (HTTP 502)")
    gh_rules(shims)
    r = run("setup-github-repo.zsh", "--config", str(dept.path), "--yes")
    assert r.returncode != 0 and "HTTP 502" in r.stderr
    assert puts(shims) == {}


def test_github_repo_error_reading_the_admin_environment_changes_it_not(run, dept, shims):
    shims.on("gh", r"^api repos/pu-orfe/chat-config/environments/orfe-admin$", exit=1, stderr="gh: Server Error (HTTP 502)")
    gh_rules(shims)
    r = run("setup-github-repo.zsh", "--config", str(dept.path), "--yes")
    assert r.returncode != 0
    assert "repos/pu-orfe/chat-config/environments/orfe-admin" not in puts(shims)


def test_bootstrap_passes_admin_reviewers_through_to_the_repo_setup(run, dept, shims, tmp_path):
    oidc_rules(shims)
    entra_app(shims, "orfe-chat-github-actions", "66666666-6666-6666-6666-666666666666")
    gh_rules(shims, users=(("me-login", 1001), ("alice", 2001)))
    shims.on("gh", r"^variable set ")
    r = run("bootstrap.zsh", "--config", str(dept.path), "--only", "github", "--set-gh-vars",
            "--admin-reviewer", "me-login", "--admin-reviewer", "alice", "--yes",
            env={"CHAT_STATE_DIR": str(tmp_path / "state")})
    assert r.returncode == 0, r.stderr
    admin = puts(shims)["repos/pu-orfe/chat-config/environments/orfe-admin"]
    assert sorted(x["id"] for x in admin["reviewers"]) == [1001, 2001] and admin["prevent_self_review"] is True


@pytest.mark.parametrize("script", ["bootstrap.zsh", "setup-github-oidc.zsh"])
def test_admin_reviewer_needs_set_gh_vars(run, dept, shims, script):
    r = run(script, "--config", str(dept.path), "--admin-reviewer", "alice")
    assert r.returncode != 0 and "--admin-reviewer only applies with --set-gh-vars" in r.stderr


# ---------------------------------------------------------------- 8. keepalive

def gated_keepalive(dept, shims, snapshot):
    (dept.path / "pugwips-snapshot.json").write_text(json.dumps(snapshot))
    az_basics(shims)
    shims.on("az", r"^containerapp env show .*defaultDomain", DOMAIN)
    shims.on("az", r"^keyvault secret show .*lab-oidc-secret --query attributes\.expires", "2099-01-01T00:00:00Z")
    shims.on("curl", r"hc-ping\.com", "OK")
    shims.on("curl", r".", exit=22)  # smoke fails too: the snapshot check must still run


def test_keepalive_reads_resolved_at_like_ip_gate(run, dept, shims):
    gated_keepalive(dept, shims, {"resolved_at": "2020-01-01T00:00:00Z", "gateways": []})
    r = run("keepalive.zsh", "--config", str(dept.path), "--server", "lab", env=SKIP_RENDER)
    assert "Traceback" not in r.stderr and "KeyError" not in r.stderr
    assert "pugwips snapshot is" in r.stderr and "days old" in r.stderr
    assert "no readable snapshot_date" not in r.stderr
    assert "keepalive failed for: lab" in r.stderr  # the smoke failure, reported at the end


def test_keepalive_snapshot_without_a_date_is_a_problem_not_a_crash(run, dept, shims):
    gated_keepalive(dept, shims, {"gateways": []})
    r = run("keepalive.zsh", "--config", str(dept.path), "--server", "lab", env=SKIP_RENDER)
    assert r.returncode != 0
    assert "has no readable snapshot_date or resolved_at" in r.stderr
    assert "Traceback" not in r.stderr and "unexpected exit" not in r.stderr
    assert "keepalive failed for: lab" in r.stderr


def test_keepalive_unreadable_secret_expiry_is_a_problem(run, dept, shims):
    keepalive_rules(shims, base=f"https://orfe-chat-dept.{DOMAIN}", expires="not-a-date")
    r = run("keepalive.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode != 0
    assert "could not read the expiry 'not-a-date' of dept-oidc-secret" in r.stderr


def test_keepalive_unreadable_smtp_expiry_is_a_problem(run, dept, shims):
    acs_dept(dept)
    shims.on("az", r"^keyvault secret show .*email-password --query attributes\.expires", "garbage")
    keepalive_rules(shims, base=f"https://orfe-chat-dept.{DOMAIN}", expires="2099-01-01T00:00:00Z")
    r = run("keepalive.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode != 0 and "could not read the expiry 'garbage' of email-password" in r.stderr


# ---------------------------------------------------------------- 9. az_retry

def test_job_start_is_not_retried_after_a_connection_reset(run, dept, shims):
    shims.on("az", r"^containerapp job start ", **TRANSIENT)
    role_rules(shims, {"ok": True, "email": "m@example.edu", "role": "moderator", "created": False})
    r = run("realm.zsh", "--config", str(dept.path), "--server", "dept", "--set-role", "_root", "m@example.edu", "moderator")
    assert r.returncode != 0
    assert len([c for c in shims.joined("az") if c.startswith("containerapp job start")]) == 1


def test_job_start_is_retried_when_throttled(run, dept, shims):
    shims.on("az", r"^containerapp job start ", exit=1, stderr="ERROR: Operation returned an invalid status 'Too Many Requests'", times=1)
    role_rules(shims, {"ok": True, "email": "m@example.edu", "role": "moderator", "created": False})
    r = run("realm.zsh", "--config", str(dept.path), "--server", "dept", "--set-role", "_root", "m@example.edu", "moderator")
    assert r.returncode == 0, r.stderr
    assert len([c for c in shims.joined("az") if c.startswith("containerapp job start")]) == 2


def test_429_inside_another_number_is_not_throttling(run, dept, shims):
    shims.on("az", r"^containerapp job start ", exit=1, stderr="ERROR: invalid port 14290", times=1)
    role_rules(shims, {"ok": True, "email": "m@example.edu", "role": "moderator", "created": False})
    r = run("realm.zsh", "--config", str(dept.path), "--server", "dept", "--set-role", "_root", "m@example.edu", "moderator")
    assert r.returncode != 0
    assert len([c for c in shims.joined("az") if c.startswith("containerapp job start")]) == 1


def test_other_commands_are_still_retried_after_a_connection_reset(run, dept, shims):
    shims.on("az", r"^containerapp revision deactivate ", **TRANSIENT, times=1)
    deploy_rules(shims)
    r = deploy(run, dept)
    assert r.returncode == 0, r.stderr
    assert len([c for c in shims.joined("az") if c.startswith("containerapp revision deactivate")]) == 2


# ---------------------------------------------------------------- 10. secrets temp file

def test_secret_temp_file_is_removed_when_az_fails(run, dept, shims):
    shims.on("az", r"^keyvault secret set ", **DENIED)
    grant_rules(shims, missing_secrets=("dept-redis-password",))
    r = run("grant-access.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode != 0 and "could not store Key Vault secret dept-redis-password" in r.stderr
    sets = [c for c in shims.calls("az") if c["args"][:3] == ["keyvault", "secret", "set"]]
    assert len(sets) == 1 and len(sets[0]["file"]) == 64  # the value reached az through the file...
    assert not Path(sets[0]["args"][sets[0]["args"].index("--file") + 1]).exists()  # ...which is gone


# ---------------------------------------------------------------- 11. saved custom domains

def test_first_deploy_without_a_save_writes_nothing(run, dept, shims):
    deploy_rules(shims, live_image=None)
    r = deploy(run, dept)
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith("keyvault secret set") for c in shims.joined("az"))


def test_a_cleared_save_restores_nothing(run, dept, shims):
    shims.on("az", r"^keyvault secret show --vault-name orfe-chat-kv --name dept-custom-domains --query id", "id")
    shims.on("az", r"^keyvault secret show --vault-name orfe-chat-kv --name dept-custom-domains --query value", "[]")
    deploy_rules(shims, live_image=None)
    r = deploy(run, dept)
    assert r.returncode == 0, r.stderr
    assert "restoring" not in r.stderr
    assert not any(c.startswith("keyvault secret set") for c in shims.joined("az"))


def test_teardown_saves_the_bindings_it_had(run, dept, shims):
    domains = [{"name": "chat.orfe.example.edu", "bindingType": "SniEnabled", "certificateId": "/c"}]
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", "/app")
    shims.on("az", r"^containerapp show .*customDomains", domains)
    shims.on("az", r"^keyvault secret show-deleted ", **NOT_FOUND)
    shims.on("az", r"^keyvault secret set ")
    shims.on("az", r"^containerapp delete ")
    shims.on("az", r"^containerapp job show ", **NOT_FOUND)
    shims.on("az", r"^ad app list ", "")
    r = run("teardown-server.zsh", "--config", str(dept.path), "--server", "dept", env={"CHAT_CONFIRM": "CONFIRM-DELETE dept"})
    assert r.returncode == 0, r.stderr
    s = next(c for c in shims.calls("az") if c["args"][:3] == ["keyvault", "secret", "set"])
    assert json.loads(s["file"]) == domains


# ---------------------------------------------------------------- per-server hc identity

def test_grant_access_creates_and_grants_the_hc_identity(run, dept, shims):
    with_hc_identity(dept)
    shims.on("az", r"^identity show -g orfe-chat-rg -n orfe-chat-dept-hc-id --query principalId", "pid-hc")
    grant_rules(shims)
    r = run("grant-access.zsh", "--config", str(dept.path), "--server", "dept", env=SKIP_RENDER)
    assert r.returncode == 0, r.stderr
    calls = shims.joined("az")
    assert "identity create -g orfe-chat-rg -n orfe-chat-dept-hc-id -l canadacentral --tags chat-server=dept --output none" in calls
    hc = [c for c in calls if c.startswith("role assignment create --assignee-object-id pid-hc ")]
    assert [c.split("--role ")[1].split(" --scope ")[0] for c in hc] == ["Key Vault Secrets User", "AcrPull"]
    assert hc[0].split("--scope ")[1].split()[0] == "/subs/x/vaults/orfe-chat-kv/secrets/healthchecks-ping-key"
    assert hc[1].split("--scope ")[1].split()[0] == "/subs/x/registries/orfechatacr"


def test_grant_access_without_hc_identity_creates_two(run, dept, shims):
    grant_rules(shims)
    r = run("grant-access.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode == 0, r.stderr
    assert not any("hc-id" in c for c in shims.joined("az"))


def test_deploy_checks_the_hc_identity(run, dept, shims):
    with_hc_identity(dept)
    deploy_rules(shims, no_identity=("orfe-chat-dept-hc-id",))
    r = deploy(run, dept, env=SKIP_RENDER)
    assert r.returncode != 0 and "orfe-chat-dept-hc-id (missing)" in r.stderr
    assert not any(c.startswith("deployment ") for c in shims.joined("az"))


def test_purge_deletes_the_hc_identity(run, dept, shims):
    with_hc_identity(dept)
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", **NOT_FOUND)
    shims.on("az", r"^containerapp job show ", **NOT_FOUND)
    shims.on("az", r"^containerapp env storage show ", **NOT_FOUND)
    shims.on("az", r"^storage share-rm show ", **NOT_FOUND)
    shims.on("az", r"^identity show ", "id")
    shims.on("az", r"^identity delete ")
    shims.on("az", r"^keyvault secret show .* --query id", **NOT_FOUND)
    shims.on("az", r"^ad app list ", "")
    r = run("teardown-server.zsh", "--config", str(dept.path), "--server", "dept", "--purge",
            env={"CHAT_CONFIRM": "PURGE dept", **SKIP_RENDER})
    assert r.returncode == 0, r.stderr
    deleted = [c.split(" -n ")[1].split()[0] for c in shims.joined("az") if c.startswith("identity delete")]
    assert deleted == ["orfe-chat-dept-id", "orfe-chat-dept-db-id", "orfe-chat-dept-hc-id"]


def test_purge_without_hc_still_removes_a_leftover_hc_identity(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", **NOT_FOUND)
    shims.on("az", r"^containerapp job show ", **NOT_FOUND)
    shims.on("az", r"^containerapp env storage show ", **NOT_FOUND)
    shims.on("az", r"^storage share-rm show ", **NOT_FOUND)
    shims.on("az", r"^identity show -g orfe-chat-rg -n orfe-chat-dept-hc-id ", "id")
    shims.on("az", r"^identity show ", **NOT_FOUND)
    shims.on("az", r"^identity delete ")
    shims.on("az", r"^keyvault secret show .* --query id", **NOT_FOUND)
    shims.on("az", r"^ad app list ", "")
    r = run("teardown-server.zsh", "--config", str(dept.path), "--server", "dept", "--purge", env={"CHAT_CONFIRM": "PURGE dept"})
    assert r.returncode == 0, r.stderr
    assert [c for c in shims.joined("az") if c.startswith("identity delete")] == \
        ["identity delete -g orfe-chat-rg -n orfe-chat-dept-hc-id --output none"]


# ---------------------------------------------------------------- 12. local.zsh state guard and CA

def test_local_down_refuses_a_directory_it_did_not_create(run, shims, tmp_path):
    state = tmp_path / "precious"
    state.mkdir()
    (state / "thesis.tex").write_text("years of work")
    r = run("local.zsh", "down", env={"CHAT_LOCAL_STATE": str(state)})
    assert r.returncode != 0 and "has no .chat-local-stack" in r.stderr
    assert (state / "thesis.tex").exists() and shims.calls() == []


@pytest.mark.parametrize("value", ["", "/", "HOME"])
def test_local_refuses_a_dangerous_state_dir(run, shims, tmp_path, value):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".chat-local-stack").write_text("")  # even with the marker
    (home / "keep").write_text("x")
    r = run("local.zsh", "down", env={"HOME": str(home), "CHAT_LOCAL_STATE": str(home) if value == "HOME" else value})
    assert r.returncode != 0
    assert (home / "keep").exists() and shims.calls() == []


def test_local_up_refuses_to_adopt_a_foreign_directory(run, shims, tmp_path):
    state = tmp_path / "other"
    state.mkdir()
    (state / "notes").write_text("x")
    shims.on("lsof", r"", exit=1)
    from conftest import FIXTURES
    r = run("local.zsh", "up", "--config", str(FIXTURES / "orfe"), env={"CHAT_LOCAL_STATE": str(state)})
    assert r.returncode != 0 and "holds files local.zsh did not create" in r.stderr
    assert shims.calls("docker-compose") == [] and shims.calls("docker") == []


def self_signed(path: Path) -> str:
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(path.with_suffix(".key")),
                    "-out", str(path), "-days", "1", "-subj", "/CN=chat-local-test"], check=True, capture_output=True)
    out = subprocess.run(["openssl", "x509", "-noout", "-fingerprint", "-sha1", "-in", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return out.split("=", 1)[1].strip().replace(":", "")


def local_state(tmp_path) -> Path:
    state = tmp_path / "s"
    (state / "secrets").mkdir(parents=True)
    (state / "override.json").write_text("{}")
    (state / ".chat-local-stack").write_text("")
    return state


def test_local_trust_records_the_ca_and_down_removes_it(run, shims, tmp_path):
    state = local_state(tmp_path)
    sha = self_signed(state / "local-ca.crt")  # what `compose cp` would have copied out
    shims.on("docker-compose", r" ps -q zulip$", "cid-1")
    shims.on("docker-compose", r" cp edge:/data/caddy/pki/authorities/local/root.crt ")
    shims.on("security", r"^add-trusted-cert -r trustRoot -k \S+login\.keychain-db \S+local-ca\.crt$")
    r = run("local.zsh", "trust", env={"CHAT_LOCAL_STATE": str(state)})
    assert r.returncode == 0, r.stderr
    assert (state / "trusted-ca.sha1").read_text().strip() == sha

    shims.on("docker-compose", r" --profile jobs down -v --remove-orphans$")
    shims.on("security", rf"^delete-certificate -Z {sha} \S+login\.keychain-db$")
    r = run("local.zsh", "down", env={"CHAT_LOCAL_STATE": str(state)})
    assert r.returncode == 0, r.stderr
    assert f"delete-certificate -Z {sha} {os.environ.get('HOME', '/tmp')}/Library/Keychains/login.keychain-db" in shims.joined("security")
    assert "removed the local CA" in r.stderr and not state.exists()


def test_local_down_warns_when_the_ca_cannot_be_removed(run, shims, tmp_path):
    state = local_state(tmp_path)
    (state / "trusted-ca.sha1").write_text("AB" * 20 + "\n")
    shims.on("docker-compose", r" --profile jobs down -v --remove-orphans$")
    shims.on("security", r"^delete-certificate ", exit=44)
    r = run("local.zsh", "down", env={"CHAT_LOCAL_STATE": str(state)})
    assert r.returncode == 0, r.stderr
    assert "could not remove the local CA" in r.stderr and "Keychain Access" in r.stderr
    assert not state.exists()


# ---------------------------------------------------------------- 13. teardown.zsh server list

def preserve_rules(shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-\w+ --query id", "/app")
    shims.on("az", r"^containerapp show .*customDomains", "[]")
    shims.on("az", r"^keyvault secret show-deleted ", **NOT_FOUND)
    shims.on("az", r"^keyvault secret set ")
    shims.on("az", r"^containerapp delete ")
    shims.on("az", r"^containerapp job show ", **NOT_FOUND)
    shims.on("az", r"^ad app list ", "")


def deleted_apps(shims):
    return [c.split(" -n ")[1].split()[0] for c in shims.joined("az") if c.startswith("containerapp delete")]


def test_teardown_repeated_server_takes_them_all(run, dept, shims):
    preserve_rules(shims)
    r = run("teardown.zsh", "--config", str(dept.path), "--server", "lab", "--server", "dept",
            env={"CHAT_CONFIRM": "TEARDOWN orfe dept lab"})
    assert r.returncode == 0, r.stderr
    assert sorted(deleted_apps(shims)) == ["orfe-chat-dept", "orfe-chat-lab"]


def test_teardown_also_server_still_works(run, dept, shims):
    preserve_rules(shims)
    r = run("teardown.zsh", "--config", str(dept.path), "--server", "lab", "--also-server", "groups",
            env={"CHAT_CONFIRM": "TEARDOWN orfe groups lab"})
    assert r.returncode == 0, r.stderr
    assert sorted(deleted_apps(shims)) == ["orfe-chat-groups", "orfe-chat-lab"]


@pytest.mark.parametrize("args, phrase, wanted", [
    ([], "TEARDOWN orfe lab", "TEARDOWN orfe ALL"),  # one server's phrase never confirms all
    (["--server", "lab", "--server", "dept"], "TEARDOWN orfe lab", "TEARDOWN orfe dept lab"),
    (["--server", "lab", "--purge"], "TEARDOWN orfe lab", "TEARDOWN orfe lab PURGE"),
    ([], "TEARDOWN orfe", "TEARDOWN orfe ALL"),
])
def test_teardown_phrase_names_what_it_removes(run, dept, shims, args, phrase, wanted):
    r = run("teardown.zsh", "--config", str(dept.path), *args, env={"CHAT_CONFIRM": phrase})
    assert r.returncode != 0 and f"did not match '{wanted}'" in r.stderr
    assert shims.calls("az") == []


def test_teardown_usage_documents_repeated_server():
    from conftest import ROOT
    head = (ROOT / "scripts" / "teardown.zsh").read_text().split("\nsource ", 1)[0]
    assert "[--server <name> ...]" in head and "TEARDOWN <dept> ALL" in head
