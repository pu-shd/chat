"""The zsh operations scripts, run for real against recording az/gh/dig/curl stand-ins."""
from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT, az_basics

DOMAIN = "happy-sea-123.canadacentral.azurecontainerapps.io"
REGISTRY = "orfechatacr.azurecr.io"
IMAGE = f"{REGISTRY}/chat@sha256:" + "b" * 64
OLD_IMAGE = f"{REGISTRY}/chat@sha256:" + "c" * 64
VERIFY_ID = "ABCDEF0123456789"


def index_of(calls: list[str], needle: str) -> int:
    for i, c in enumerate(calls):
        if needle in c:
            return i
    raise AssertionError(f"no call containing {needle!r} in:\n" + "\n".join(calls))


# ---------------------------------------------------------------- syntax


@pytest.mark.parametrize("script", sorted(p.name for p in (ROOT / "scripts").glob("*.zsh")))
def test_zsh_scripts_parse(script):
    r = subprocess.run(["zsh", "-n", str(ROOT / "scripts" / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", ["chat-entrypoint", "chat-manage"])
def test_image_bash_scripts_parse(script):
    r = subprocess.run(["bash", "-n", str(ROOT / "image" / "bin" / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_dbinit_parses_as_posix_sh():
    r = subprocess.run(["sh", "-n", str(ROOT / "image" / "bin" / "chat-dbinit")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- deploy-server


def deploy_rules(sh, *, live_image=OLD_IMAGE, missing=(), custom_domains=None, no_identity=(), no_access=()):
    az_basics(sh)
    sh.on("az", r"^acr show -n orfechatacr -g orfe-chat-rg --query loginServer", REGISTRY)
    sh.on("az", r"^acr show -n orfechatacr -g orfe-chat-rg --query id", "/subs/x/registries/orfechatacr")
    sh.on("az", r"^containerapp env show -g orfe-chat-rg -n orfe-chat-env --query id", "/env")
    for name in missing:
        sh.on("az", rf"^keyvault secret show --vault-name orfe-chat-kv --name {name} --query id", exit=3)
    sh.on("az", r"^keyvault secret show --vault-name orfe-chat-kv --name \S+ --query id", "https://kv/secret")
    sh.on("az", r"^keyvault secret show-deleted ", exit=3)
    sh.on("az", r"^keyvault secret set ")
    sh.on("az", r"^keyvault secret show --vault-name orfe-chat-kv --name dept-custom-domains", exit=3)
    sh.on("az", r"^keyvault show -n orfe-chat-kv ", "/subs/x/vaults/orfe-chat-kv")
    for ident in no_identity:
        sh.on("az", rf"^identity show -g orfe-chat-rg -n {ident} ", exit=3)
    sh.on("az", r"^identity show -g orfe-chat-rg -n \S+ --query principalId", "pid-1")
    for secret in no_access:
        sh.on("az", rf"^role assignment list .*--scope /subs/x/vaults/orfe-chat-kv/secrets/{secret} ", "")
    sh.on("az", r"^role assignment list ", "/ra/1")
    sh.on("az", r"^containerapp job show ", exit=3)
    sh.on("az", r"^containerapp env show .*defaultDomain", DOMAIN)
    if live_image is None:
        sh.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", exit=3)
    else:
        sh.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", "/app")
        sh.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept -o json", {"properties": {
            "template": {"containers": [{"name": "zulip", "image": live_image}, {"name": "redis", "image": "r"}]},
            "configuration": {"ingress": {"customDomains": custom_domains or []}}}})
    sh.on("az", r"^deployment group create .* -n chat-dept-infra ", {})
    sh.on("az", r"^deployment group create .* -n chat-dept-app ", {
        "appFqdn": {"value": f"orfe-chat-dept.{DOMAIN}"}, "latestRevision": {"value": "orfe-chat-dept--r2"}})
    sh.on("az", r"^containerapp job start --name orfe-chat-dept-dbinit ", "exec-db")
    sh.on("az", r"^containerapp job start --name orfe-chat-dept-mgmt ", "exec-mgmt")
    sh.on("az", r"^containerapp job execution show ", "Succeeded")
    sh.on("az", r"^containerapp job logs show --name orfe-chat-dept-dbinit ",
          'noise\nCHAT-RESULT: {"ok": true, "database": "zulip_dept", "collation": "C.UTF-8"}')
    sh.on("az", r"^containerapp job logs show --name orfe-chat-dept-mgmt ",
          f'CHAT-RESULT: {{"ok": true, "slug": "", "created": true, "url": "https://orfe-chat-dept.{DOMAIN}"}}')
    sh.on("az", r"^monitor log-analytics workspace show", exit=3)
    sh.on("az", r"^containerapp revision list .*properties\.active", "orfe-chat-dept--r1")
    sh.on("az", r"^containerapp revision deactivate ")
    sh.on("az", r"^containerapp revision show ", "Healthy\tRunning")
    sh.on("az", r"^containerapp auth show ", exit=3)
    return sh


def deploy(run, dept, *extra, env=None):
    return run("deploy-server.zsh", "--config", str(dept.path), "--server", "dept", "--image", IMAGE, *extra, env=env)


def test_deploy_orders_db_before_app_and_stops_old_revision_on_upgrade(run, dept, shims):
    deploy_rules(shims)
    r = deploy(run, dept)
    assert r.returncode == 0, r.stderr
    calls = shims.joined("az")
    infra = index_of(calls, "-n chat-dept-infra")
    dbinit = index_of(calls, "job start --name orfe-chat-dept-dbinit")
    stop = index_of(calls, "revision deactivate -g orfe-chat-rg -n orfe-chat-dept --revision orfe-chat-dept--r1")
    app = index_of(calls, "-n chat-dept-app")
    realm = index_of(calls, "job start --name orfe-chat-dept-mgmt")
    assert infra < dbinit < stop < app < realm
    assert "chat:manage ensure-realm _root ORFE admin@example.edu Admin" in calls[realm]
    assert "realm (root): created" in r.stderr


def test_deploy_parameters_target_the_pre_dns_name_and_keep_bound_domains(run, dept, shims):
    domains = [{"name": "chat.orfe.example.edu", "bindingType": "SniEnabled", "certificateId": "/certs/x"}]
    deploy_rules(shims, custom_domains=domains)
    r = deploy(run, dept)
    assert r.returncode == 0, r.stderr
    app_call = next(c for c in shims.calls("az") if "chat-dept-app" in " ".join(c["args"]))
    params = json.loads(next(iter(app_call["at_files"].values())))["parameters"]
    env = {e["name"]: e["value"] for e in params["zulipEnv"]["value"]}
    assert env["SETTING_EXTERNAL_HOST"] == f"orfe-chat-dept.{DOMAIN}"
    assert params["customDomains"]["value"] == domains
    assert params["image"]["value"] == IMAGE
    infra_call = next(c for c in shims.calls("az") if "chat-dept-infra" in " ".join(c["args"]))
    assert json.loads(next(iter(infra_call["at_files"].values())))["parameters"]["deployApp"]["value"] is False


def test_same_image_is_a_rolling_update(run, dept, shims):
    deploy_rules(shims, live_image=IMAGE)
    r = deploy(run, dept)
    assert r.returncode == 0, r.stderr
    assert not any("revision deactivate" in c for c in shims.joined("az"))


def test_first_deploy(run, dept, shims):
    deploy_rules(shims, live_image=None)
    r = deploy(run, dept)
    assert r.returncode == 0, r.stderr
    assert "first deploy of orfe-chat-dept" in r.stderr


def test_deploy_never_generates_secrets(run, dept, shims):
    deploy_rules(shims, missing=("dept-secret-key",))
    r = deploy(run, dept)
    assert r.returncode != 0 and "lacks dept-secret-key" in r.stderr and "grant-access.zsh --server dept" in r.stderr
    assert not any(c.startswith("keyvault secret set") for c in shims.joined("az"))


def test_deploy_refuses_without_least_privilege_access(run, dept, shims):
    deploy_rules(shims, no_identity=("orfe-chat-dept-db-id",), no_access=("email-password",))
    r = deploy(run, dept)
    assert r.returncode != 0
    assert "orfe-chat-dept-db-id (missing)" in r.stderr and "orfe-chat-dept-id → email-password" in r.stderr
    assert not any(c.startswith("deployment ") for c in shims.joined("az"))


def test_deploy_restores_bindings_saved_at_teardown(run, dept, shims):
    saved = [{"name": "chat.orfe.example.edu", "bindingType": "SniEnabled", "certificateId": "/c"}]
    shims.on("az", r"^keyvault secret show --vault-name orfe-chat-kv --name dept-custom-domains --query value", json.dumps(saved))
    deploy_rules(shims, live_image=None)
    r = deploy(run, dept)
    assert r.returncode == 0, r.stderr
    app_call = next(c for c in shims.calls("az") if "chat-dept-app" in " ".join(c["args"]))
    assert json.loads(next(iter(app_call["at_files"].values())))["parameters"]["customDomains"]["value"] == saved


def test_deploy_rejects_a_result_for_the_wrong_database(run, dept, shims):
    shims.on("az", r"^containerapp job logs show --name orfe-chat-dept-dbinit ",
             'CHAT-RESULT: {"ok": true, "database": "zulip_other", "collation": "C.UTF-8"}')
    deploy_rules(shims)
    r = deploy(run, dept)
    assert r.returncode != 0 and "does not satisfy" in r.stderr


def test_az_warnings_on_stderr_do_not_corrupt_parsed_output(run, dept, shims):
    shims.on("az", r"^deployment group create .* -n chat-dept-app ", {
        "appFqdn": {"value": f"orfe-chat-dept.{DOMAIN}"}, "latestRevision": {"value": "orfe-chat-dept--r2"}},
        stderr="WARNING: A new Bicep release is available: v9.9.9.")
    deploy_rules(shims)
    r = deploy(run, dept)
    assert r.returncode == 0, r.stderr


def test_sustained_unhealthy_fails(run, dept, shims):
    shims.on("az", r"^containerapp revision show ", "Unhealthy\tRunning")
    shims.on("az", r"^containerapp logs show ", "boom")
    deploy_rules(shims)
    r = deploy(run, dept, env={"UNHEALTHY_LIMIT": "2"})
    assert r.returncode != 0 and "is Unhealthy" in r.stderr
    assert len([c for c in shims.joined("az") if c.startswith("containerapp revision show")]) == 2


def test_missing_operator_secret_stops_before_any_deployment(run, dept, shims):
    deploy_rules(shims, missing=("dept-oidc-secret", "email-password"))
    r = deploy(run, dept)
    assert r.returncode != 0
    assert "lacks dept-oidc-secret, email-password" in r.stderr
    assert not any(c.startswith("deployment ") for c in shims.joined("az"))


def test_unpinned_image_refused_before_any_deployment(run, dept, shims):
    deploy_rules(shims)
    r = run("deploy-server.zsh", "--config", str(dept.path), "--server", "dept", "--image", f"{REGISTRY}/chat:latest")
    assert r.returncode != 0 and "by digest" in r.stderr
    assert not any(c.startswith("deployment ") for c in shims.joined("az"))


def test_stale_generated_refused(run, dept, shims):
    dept.edit(lambda c: c["servers"]["dept"].update(cpu=3.0))
    r = deploy(run, dept)
    assert r.returncode != 0 and "is stale" in r.stderr
    assert shims.calls() == []


def test_failed_dbinit_stops_the_deploy(run, dept, shims):
    shims.on("az", r"^containerapp job logs show --name orfe-chat-dept-dbinit ",
             'CHAT-RESULT: {"ok": false, "error": "database zulip_dept has collation \'en_US.utf8\', Zulip needs C.UTF-8"}')
    deploy_rules(shims)
    r = deploy(run, dept)
    assert r.returncode != 0 and "Zulip needs C.UTF-8" in r.stderr
    assert not any("chat-dept-app" in c for c in shims.joined("az"))


def test_job_without_result_line_is_a_failure(run, dept, shims):
    shims.on("az", r"^containerapp job logs show --name orfe-chat-dept-dbinit ", "all good, probably")
    shims.on("az", r"^monitor log-analytics workspace show", exit=3)
    deploy_rules(shims)
    r = deploy(run, dept, env={"JOB_LOG_TRIES": "1"})
    assert r.returncode != 0 and "reported no CHAT-RESULT" in r.stderr


def test_failed_revision_fails_at_once(run, dept, shims):
    shims.on("az", r"^containerapp revision show ", "Unhealthy\tFailed")
    shims.on("az", r"^containerapp logs show ", "boom")
    deploy_rules(shims)
    r = deploy(run, dept)
    assert r.returncode != 0 and "Failed" in r.stderr


# ---------------------------------------------------------------- teardown


def test_teardown_needs_typed_confirmation(run, dept, shims):
    az_basics(shims)
    r = run("teardown-server.zsh", "--config", str(dept.path), "--server", "dept", "--yes")
    assert r.returncode != 0 and "type-to-confirm needed" in r.stderr
    assert not any("delete" in c for c in shims.joined("az"))


def test_teardown_wrong_phrase(run, dept, shims):
    az_basics(shims)
    r = run("teardown-server.zsh", "--config", str(dept.path), "--server", "dept",
            env={"CHAT_CONFIRM": "CONFIRM-DELETE groups"})
    assert r.returncode != 0 and "did not match 'CONFIRM-DELETE dept'" in r.stderr
    assert not any("delete" in c for c in shims.joined("az"))


def test_teardown_preserve_keeps_data(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", "/app")
    shims.on("az", r"^containerapp delete ")
    shims.on("az", r"^containerapp job show ", "{}")
    shims.on("az", r"^containerapp job delete ")
    shims.on("az", r"^ad app list ", "")
    r = run("teardown-server.zsh", "--config", str(dept.path), "--server", "dept",
            env={"CHAT_CONFIRM": "CONFIRM-DELETE dept"})
    assert r.returncode == 0, r.stderr
    calls = shims.joined("az")
    assert any(c.startswith("containerapp delete -g orfe-chat-rg -n orfe-chat-dept ") for c in calls)
    assert any("job delete -g orfe-chat-rg -n orfe-chat-dept-mgmt" in c for c in calls)
    assert not any(s in c for c in calls for s in ("dbinit --yes", "share-rm", "secret delete", "DB_ACTION"))


def test_teardown_purge_drops_database_first(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", exit=3)
    shims.on("az", r"^containerapp job show ", "{}")
    shims.on("az", r"^containerapp job delete ")
    shims.on("az", r"^containerapp job start .*--env-vars DB_ACTION=drop", "exec-drop")
    shims.on("az", r"^containerapp job execution show ", "Succeeded")
    shims.on("az", r"^containerapp job logs show ", 'CHAT-RESULT: {"ok": true, "database": "zulip_dept", "dropped": true}')
    shims.on("az", r"^containerapp env storage (show|remove) ")
    shims.on("az", r"^storage share-rm (show|delete) ")
    shims.on("az", r"^identity show ", "{}")
    shims.on("az", r"^identity delete ")
    shims.on("az", r"^keyvault secret show-deleted ", exit=3)
    shims.on("az", r"^keyvault secret show .* --query id", "id")
    shims.on("az", r"^keyvault secret delete ")
    shims.on("az", r"^ad app list ", "33333333-3333-3333-3333-333333333333")
    r = run("teardown-server.zsh", "--config", str(dept.path), "--server", "dept", "--purge",
            env={"CHAT_CONFIRM": "PURGE dept"})
    assert r.returncode == 0, r.stderr
    calls = shims.joined("az")
    drop = index_of(calls, "DB_ACTION=drop")
    assert drop < index_of(calls, "share-rm delete") < index_of(calls, "secret delete")
    deleted = [c.split("--name ")[1].split()[0] for c in calls if "secret delete" in c]
    assert "email-password" not in deleted and "pg-admin-password" not in deleted  # department-wide
    assert "dept-secret-key" in deleted and "dept-oidc-client-id" in deleted
    assert "az ad app delete --id 33333333-3333-3333-3333-333333333333" in r.stderr


# ---------------------------------------------------------------- bind-domain


def bind_rules(sh):
    az_basics(sh)
    sh.on("az", r"^containerapp env show .*defaultDomain", DOMAIN)
    sh.on("az", r"^containerapp env show .*customDomainVerificationId", VERIFY_ID)
    return sh


def test_dns_ticket_lists_cname_and_txt_per_host(run, dept, shims):
    bind_rules(shims)
    r = run("bind-domain.zsh", "--config", str(dept.path), "--server", "groups", "--print")
    assert r.returncode == 0, r.stderr
    for host in ["groups.chat.orfe.example.edu", "auth.groups.chat.orfe.example.edu",
                 "ahmadi-group.chat.orfe.example.edu", "beta-lab.chat.orfe.example.edu"]:
        assert f"| CNAME | `{host}` | `orfe-chat-groups.{DOMAIN}` | 3600 |" in r.stdout
        assert f"| TXT | `asuid.{host}` | `{VERIFY_ID}` | 3600 |" in r.stdout
    assert f"reachable at https://orfe-chat-groups.{DOMAIN}/" in r.stdout
    assert shims.calls("dig") == []


def test_bind_without_dns_exits_4_and_binds_nothing(run, dept, shims):
    bind_rules(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", "/app")
    shims.on("dig", r".", "")
    r = run("bind-domain.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode == 4
    assert "DNS not in place yet for: chat.orfe.example.edu" in r.stderr
    assert not any("hostname bind" in c for c in shims.joined("az"))


def test_bind_with_dns_uses_managed_certificate(run, dept, shims):
    bind_rules(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-dept --query id", "/app")
    shims.on("az", r"^containerapp hostname list ", "")
    shims.on("az", r"^containerapp hostname bind ")
    shims.on("dig", r"^\+short CNAME chat\.orfe\.example\.edu", f"orfe-chat-dept.{DOMAIN}.")
    shims.on("dig", r"^\+short TXT asuid\.chat\.orfe\.example\.edu", f'"{VERIFY_ID}"')
    r = run("bind-domain.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode == 0, r.stderr
    binds = [c for c in shims.joined("az") if "hostname bind" in c]
    assert binds == ["containerapp hostname bind -g orfe-chat-rg -n orfe-chat-dept --hostname chat.orfe.example.edu "
                     "--environment orfe-chat-env --validation-method CNAME --output none"]


def test_gated_server_refuses_managed_certificate(run, dept, shims):
    bind_rules(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-lab --query id", "/app")
    r = run("bind-domain.zsh", "--config", str(dept.path), "--server", "lab")
    assert r.returncode != 0 and "DigiCert cannot reach it" in r.stderr


# ---------------------------------------------------------------- entra-app


def test_entra_app_sets_pre_dns_and_live_redirects_and_vaults_secret(run, dept, shims):
    az_basics(shims)
    cid = "22222222-2222-2222-2222-222222222222"
    shims.on("az", r"^ad app list --display-name orfe-chat-groups-zulip", cid)
    shims.on("az", r"^containerapp env show .*defaultDomain", DOMAIN)
    shims.on("az", r"^ad app show --id .* web\.redirectUris", "")
    shims.on("az", r"^ad app update ")
    shims.on("az", r"^ad sp show --id \S+ --query id", "sp-object-id")
    shims.on("az", r"^ad sp show --id ", "{}")
    shims.on("az", r"^ad sp update ")
    shims.on("az", r"^keyvault secret show .*groups-oidc-secret --query id", exit=3)
    shims.on("az", r"^ad app credential reset ", {"password": "S3CRET-VALUE-9", "end": "2028-09-30T12:00:00Z"})
    shims.on("az", r"^keyvault secret set ")
    shims.on("az", r"^keyvault secret show .*groups-oidc-client-id --query value", exit=3)
    r = run("entra-app.zsh", "--config", str(dept.path), "--server", "groups")
    assert r.returncode == 0, r.stderr
    upd = next(c for c in shims.joined("az") if "--web-redirect-uris" in c)
    assert "https://auth.groups.chat.orfe.example.edu/complete/oidc/" in upd
    assert f"https://orfe-chat-groups.{DOMAIN}/complete/oidc/" in upd
    sets = {c["args"][c["args"].index("--name") + 1]: c["file"] for c in shims.calls("az") if c["args"][:3] == ["keyvault", "secret", "set"]}
    assert sets["groups-oidc-secret"] == "S3CRET-VALUE-9"
    secret_set = next(c for c in shims.joined("az") if "--name groups-oidc-secret --file" in c)
    assert "--expires 2028-09-30T12:00:00Z" in secret_set  # keepalive reads this
    assert sets["groups-oidc-client-id"] == cid
    assert "S3CRET-VALUE-9" not in r.stdout + r.stderr
    assert "no Entra group assigned" in r.stderr
    assert any("appRoleAssignmentRequired=true" in c for c in shims.joined("az"))


def test_entra_app_refuses_mismatched_pinned_client_id(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^ad app list --display-name orfe-chat-dept-zulip", "99999999-9999-9999-9999-999999999999")
    r = run("entra-app.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode != 0 and "pins entra.client_id=11111111" in r.stderr


# ---------------------------------------------------------------- smoke


def test_smoke_checks_realm_login_and_redirects(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp env show .*defaultDomain", DOMAIN)
    base = f"https://orfe-chat-dept.{DOMAIN}"
    shims.on("curl", rf"{base}/api/v1/server_settings", {"result": "success", "realm_url": base})
    shims.on("curl", r"url_effective", "https://login.microsoftonline.com/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/oauth2/v2.0/authorize?x")
    shims.on("curl", rf"{base}/ahmadi-group$", f"302 https://orfe-chat-groups.{DOMAIN}/")
    shims.on("curl", rf"{base}/lab$", f"302 https://orfe-chat-lab.{DOMAIN}/")
    r = run("smoke.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode == 0, r.stderr
    assert "skip /beta-lab: target not reachable before DNS" in r.stderr
    assert f"{base}/ahmadi-group → https://orfe-chat-groups.{DOMAIN}/" in r.stderr


def test_smoke_fails_on_wrong_redirect(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp env show .*defaultDomain", DOMAIN)
    base = f"https://orfe-chat-dept.{DOMAIN}"
    shims.on("curl", rf"{base}/api/v1/server_settings", {"result": "success", "realm_url": base})
    shims.on("curl", r"url_effective", "https://login.microsoftonline.com/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/x")
    shims.on("curl", r"/ahmadi-group$", "404 ")
    shims.on("curl", r"/lab$", f"302 https://orfe-chat-lab.{DOMAIN}/")
    r = run("smoke.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode != 0 and "1 smoke check(s) failed" in r.stderr


# ---------------------------------------------------------------- bootstrap


def test_bootstrap_rejects_unknown_step(run, dept):
    r = run("bootstrap.zsh", "--config", str(dept.path), "--from", "bogus")
    assert r.returncode != 0 and "unknown step 'bogus'" in r.stderr and "prereqs platform image github" in r.stderr


def test_bootstrap_dns_step_writes_ticket_with_redirect_host_last(run, dept, shims, tmp_path):
    bind_rules(shims)
    r = run("bootstrap.zsh", "--config", str(dept.path), "--only", "dns",
            env={"CHAT_OUT_DIR": str(tmp_path), "CHAT_STATE_DIR": str(tmp_path / "state")})
    assert r.returncode == 0, r.stderr
    ticket = (tmp_path / "dns-request-orfe.md").read_text()
    order = [ticket.index(f"DNS request: {s} ") for s in ("groups", "lab", "dept")]
    assert order == sorted(order)
    assert f"| CNAME | `chat.orfe.example.edu` | `orfe-chat-dept.{DOMAIN}` | 3600 |" in ticket


# ---------------------------------------------------------------- image entrypoint


def entrypoint(tmp_path, b64: str | None):
    conf = tmp_path / "app.d" / "chat-redirects.conf"
    env = {**os.environ, "CHAT_ENTRYPOINT_TEST": "1", "CHAT_REDIRECTS_CONF": str(conf)}
    if b64 is not None:
        env["CHAT_REDIRECTS_B64"] = b64
    r = subprocess.run(["bash", str(ROOT / "image" / "bin" / "chat-entrypoint")], env=env, capture_output=True, text=True)
    return r, conf


def test_entrypoint_installs_rendered_redirects(dept, capsys, tmp_path):
    import render
    render.main(["resolve", "--server", str(dept.path / "generated/servers/dept.json"), "--default-domain", DOMAIN,
                 "--image", IMAGE, "--registry", REGISTRY])
    params = json.loads(capsys.readouterr().out)["parameters"]
    b64 = next(e["value"] for e in params["zulipEnv"]["value"] if e["name"] == "CHAT_REDIRECTS_B64")
    r, conf = entrypoint(tmp_path, b64)
    assert r.returncode == 0, r.stderr
    assert "installed 2 redirect(s)" in r.stdout
    assert conf.read_text() == base64.b64decode(b64).decode()


@pytest.mark.parametrize("payload", [
    "location / { proxy_pass http://evil; }\n",
    "location ~ ^/x/?$ { return 302 https://ok.example/; }\nadd_header X-Evil 1;\n",
])
def test_entrypoint_refuses_anything_but_redirects(tmp_path, payload):
    r, conf = entrypoint(tmp_path, base64.b64encode(payload.encode()).decode())
    assert r.returncode == 0  # never blocks Zulip from starting...
    assert "refusing it" in r.stderr
    assert not conf.exists()  # ...but installs nothing


def test_entrypoint_rejects_bad_base64_and_clears_when_unset(tmp_path):
    r, conf = entrypoint(tmp_path, "!!!not base64!!!")
    assert "not valid base64" in r.stderr and not conf.exists()
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("stale")
    r, conf = entrypoint(tmp_path, None)
    assert r.returncode == 0 and not conf.exists()


def test_image_from_another_registry_is_refused(run, dept, shims):
    deploy_rules(shims)
    r = run("deploy-server.zsh", "--config", str(dept.path), "--server", "dept",
            "--image", "evil.azurecr.io/chat@sha256:" + "e" * 64)
    assert r.returncode != 0 and f"image must come from {REGISTRY} by digest" in r.stderr
    assert not any(c.startswith("deployment ") for c in shims.joined("az"))


def test_default_image_is_the_locked_tag_in_the_registry(run, dept, shims, tmp_path):
    lock = tmp_path / "template.lock"
    lock.write_text(json.dumps({"repo": "pu-shd/chat", "ref": "v0.3.0", "sha": "abcdef1" + "0" * 33}))
    shims.on("az", r"^acr repository show -n orfechatacr --image chat:v0\.3\.0-abcdef1 --query digest", "sha256:" + "d" * 64)
    deploy_rules(shims)
    r = run("deploy-server.zsh", "--config", str(dept.path), "--server", "dept", "--lock", str(lock))
    assert r.returncode == 0, r.stderr
    app_call = next(c for c in shims.calls("az") if "chat-dept-app" in " ".join(c["args"]))
    params = json.loads(next(iter(app_call["at_files"].values())))["parameters"]
    assert params["image"]["value"] == f"{REGISTRY}/chat@sha256:" + "d" * 64
    assert params["registryServer"]["value"] == REGISTRY


def test_unbuilt_image_stops_the_deploy(run, dept, shims, tmp_path):
    lock = tmp_path / "template.lock"
    lock.write_text(json.dumps({"repo": "pu-shd/chat", "ref": "v0.3.0", "sha": "abcdef1" + "0" * 33}))
    shims.on("az", r"^acr repository show ", exit=3)
    deploy_rules(shims)
    r = run("deploy-server.zsh", "--config", str(dept.path), "--server", "dept", "--lock", str(lock))
    assert r.returncode != 0 and "chat:v0.3.0-abcdef1 is not built yet" in r.stderr


# ---------------------------------------------------------------- bootstrap state and resume


def template_sha():
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or "unknown"


def state(tmp_path):
    f = tmp_path / "state" / "orfe.state"
    return json.loads(f.read_text()) if f.exists() else {}


def test_bootstrap_records_each_step(run, dept, shims, tmp_path):
    bind_rules(shims)
    r = run("bootstrap.zsh", "--config", str(dept.path), "--only", "dns",
            env={"CHAT_OUT_DIR": str(tmp_path), "CHAT_STATE_DIR": str(tmp_path / "state")})
    assert r.returncode == 0, r.stderr
    st = state(tmp_path)
    assert st["dns"]["status"] == "done" and st["dns"]["template"] == template_sha()
    assert "▶ [1/1] dns" in r.stderr and "✓ dns" in r.stderr and "Summary" in r.stderr


def test_bootstrap_failure_is_recorded_with_a_resume_hint(run, dept, shims, tmp_path):
    az_basics(shims)
    shims.on("az", r"^containerapp env show .*defaultDomain", DOMAIN)
    shims.on("curl", r".", exit=22)
    r = run("bootstrap.zsh", "--config", str(dept.path), "--only", "smoke",
            env={"CHAT_STATE_DIR": str(tmp_path / "state")})
    assert r.returncode == 1
    assert state(tmp_path)["smoke"]["status"] == "failed"
    assert "✗ smoke failed" in r.stderr and "Stopped at smoke" in r.stderr and "--resume" in r.stderr


def test_bootstrap_resume_skips_what_is_done(run, dept, shims, tmp_path):
    (tmp_path / "state").mkdir()
    done = {s: {"status": "done", "seconds": 1, "template": template_sha(), "at": "2026-09-30T00:00:00Z"}
            for s in ["prereqs", "platform", "image", "github", "secrets", "entra", "access", "servers",
                      "healthchecks", "smoke"]}
    (tmp_path / "state" / "orfe.state").write_text(json.dumps(done))
    bind_rules(shims)
    r = run("bootstrap.zsh", "--config", str(dept.path), "--resume",
            env={"CHAT_OUT_DIR": str(tmp_path), "CHAT_STATE_DIR": str(tmp_path / "state")})
    assert r.returncode == 0, r.stderr
    assert "▶ [1/1] dns" in r.stderr  # only the unfinished step ran
    assert state(tmp_path)["dns"]["status"] == "done"
    r = run("bootstrap.zsh", "--config", str(dept.path), "--resume", env={"CHAT_STATE_DIR": str(tmp_path / "state")})
    assert r.returncode == 0 and "every step is done" in r.stderr


def test_bootstrap_treats_steps_from_another_template_commit_as_stale(run, dept, shims, tmp_path):
    (tmp_path / "state").mkdir()
    old = {s: {"status": "done", "seconds": 1, "template": "0" * 40, "at": "x"}
           for s in ["prereqs", "platform", "image", "github", "secrets", "entra", "access", "servers",
                     "healthchecks", "smoke", "dns"]}
    (tmp_path / "state" / "orfe.state").write_text(json.dumps(old))
    az_basics(shims)
    r = run("bootstrap.zsh", "--config", str(dept.path), "--resume", env={"CHAT_STATE_DIR": str(tmp_path / "state")})
    assert "↻" in r.stderr and "stale" in r.stderr
    assert "▶ [1/11] prereqs" in r.stderr  # resumes from the first stale step


def test_bootstrap_restart_forgets_progress(run, dept, shims, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "orfe.state").write_text(json.dumps({"dns": {"status": "done", "template": template_sha()}}))
    bind_rules(shims)
    r = run("bootstrap.zsh", "--config", str(dept.path), "--restart", "--only", "dns",
            env={"CHAT_OUT_DIR": str(tmp_path), "CHAT_STATE_DIR": str(tmp_path / "state")})
    assert r.returncode == 0, r.stderr
    assert "Progress so far" not in r.stderr and list(state(tmp_path)) == ["dns"]


# ---------------------------------------------------------------- themes (entrypoint)


def themes(tmp_path, value, themes_dir=None):
    th, ts = tmp_path / "conf.d" / "chat-themes.conf", tmp_path / "app.d" / "chat-themes.conf"
    env = {**os.environ, "CHAT_ENTRYPOINT_TEST": "1", "CHAT_REDIRECTS_CONF": str(tmp_path / "r.conf"),
           "CHAT_RATE_HTTP_CONF": str(tmp_path / "h.conf"), "CHAT_RATE_SERVER_CONF": str(tmp_path / "s.conf"),
           "CHAT_THEMES_DIR": str(themes_dir or ROOT / "image" / "themes"),
           "CHAT_THEME_HTTP_CONF": str(th), "CHAT_THEME_SERVER_CONF": str(ts)}
    if value is not None:
        env["CHAT_THEMES"] = value
    r = subprocess.run(["bash", str(ROOT / "image" / "bin" / "chat-entrypoint")], env=env, capture_output=True, text=True)
    return r, th, ts


def test_theme_map_and_injection_are_generated(tmp_path):
    r, th, ts = themes(tmp_path, "chat.orfe.example.edu=paper-tiger orfe-chat-dept.x.io=paper-tiger")
    assert r.returncode == 0 and "theme(s) for 2 host(s)" in r.stdout
    http = th.read_text()
    assert "map $host $chat_theme_link {" in http and 'default "";' in http
    assert "    chat.orfe.example.edu '<link rel=\"stylesheet\" href=\"/chat-theme/paper-tiger/theme.css?v=" in http
    server = ts.read_text()
    assert "sub_filter '</head>' '$chat_theme_link</head>';" in server
    assert "include /etc/nginx/zulip-include/headers;" in server


@pytest.mark.parametrize("value", ["chat.orfe.example.edu=neon", "chat.orfe.example.edu",
                                   "chat.orfe.example.edu=paper-tiger;evil", "Bad_Host=paper-tiger",
                                   "chat.orfe.example.edu=../../etc"])
def test_bad_theme_entries_turn_themes_off(tmp_path, value):
    r, th, ts = themes(tmp_path, value)
    assert r.returncode == 0 and "themes OFF" in r.stderr
    assert not th.exists() and not ts.exists()


def test_no_themes_removes_old_files(tmp_path):
    themes(tmp_path, "chat.orfe.example.edu=paper-tiger")
    r, th, ts = themes(tmp_path, None)
    assert "Zulip default look" in r.stdout and not th.exists() and not ts.exists()


def test_every_theme_follows_the_rules():
    import re
    for css in (ROOT / "image" / "themes").glob("*/theme.css"):
        text = css.read_text()
        body = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        assert "url(" not in body and "@import" not in body, f"{css}: no external or file references"
        selectors = {s.strip() for s in re.findall(r"([^{}]+)\{", body)}
        assert selectors <= {":root", "::selection"}, f"{css}: tokens only, found {selectors}"
        assert "light-dark(" in body, f"{css}: needs dark values"
