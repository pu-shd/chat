"""healthchecks.zsh, keepalive.zsh, teardown.zsh — run for real against recording stand-ins."""
from __future__ import annotations

import datetime as dt
import json
import subprocess

import pytest

from conftest import ROOT, az_basics
from test_scripts import DOMAIN, IMAGE, deploy_rules

API_KEY = "hc-API-SENTINEL-77"
PING_KEY = "hc-PING-SENTINEL-88"


def enable_hc(dept):
    dept.edit(lambda c: c.update(healthchecks={"enabled": True}))
    dept.render()


# ---------------------------------------------------------------- healthchecks.zsh


def test_sync_creates_missing_and_updates_existing(run, dept, shims):
    enable_hc(dept)
    shims.on("curl", r"GET .*/checks/\?slug=orfe-chat-dept-health",
             {"checks": [{"update_url": "https://healthchecks.io/api/v3/checks/u-1", "status": "up"}]})
    shims.on("curl", r"GET .*/checks/\?slug=", {"checks": []})
    shims.on("curl", r"POST", {})
    r = run("healthchecks.zsh", "--config", str(dept.path), "--sync", env={"HEALTHCHECKS_API_KEY": API_KEY})
    assert r.returncode == 0, r.stderr
    posts = [c for c in shims.calls("curl") if "POST" in c["args"]]
    targets = [c["args"][-1] for c in posts]
    assert targets.count("https://healthchecks.io/api/v3/checks/") == 7
    assert "https://healthchecks.io/api/v3/checks/u-1" in targets
    upd = next(c for c in posts if c["args"][-1].endswith("/u-1"))
    body = json.loads(upd["args"][upd["args"].index("--data-raw") + 1])
    assert body == {"name": "dept: Zulip /health", "slug": "orfe-chat-dept-health",
                    "desc": body["desc"], "timeout": 300, "grace": 900, "tags": "chat orfe dept"}
    assert "updated orfe-chat-dept-health" in r.stderr and "created orfe-chat-lab-ip-gate" in r.stderr
    # The API key travels in a header file, never on a command line.
    assert not any(API_KEY in a for c in shims.calls() for a in c["args"])
    assert all(any(API_KEY in v for v in c.get("at_files", {}).values()) for c in shims.calls("curl"))


def test_sync_without_api_key_fails_loudly(run, dept, shims):
    enable_hc(dept)
    az_basics(shims)
    shims.on("az", r"^keyvault secret show .*healthchecks-api-key", exit=3)
    r = run("healthchecks.zsh", "--config", str(dept.path), "--sync")
    assert r.returncode != 0 and "no Healthchecks API key" in r.stderr


def test_disabled_is_a_visible_noop(run, dept, shims):
    r = run("healthchecks.zsh", "--config", str(dept.path), "--sync")
    assert r.returncode == 0 and "healthchecks.enabled is false" in r.stderr
    assert shims.calls() == []


def test_ping_uses_key_and_slug(run, dept, shims):
    enable_hc(dept)
    shims.on("curl", r"hc-ping\.com", "OK")
    r = run("healthchecks.zsh", "--config", str(dept.path), "--ping", "orfe-chat-updates", "--fail",
            "--message", "boom", env={"HEALTHCHECKS_PING_KEY": PING_KEY})
    assert r.returncode == 0, r.stderr
    call = shims.calls("curl")[0]
    assert call["stdin"].strip() == f'url = "https://hc-ping.com/{PING_KEY}/orfe-chat-updates/fail?create=1"'
    assert call["args"][call["args"].index("--data-raw") + 1] == "boom"
    assert not any(PING_KEY in a for a in call["args"])  # the key never reaches argv


def test_ping_unknown_slug_refused(run, dept, shims):
    enable_hc(dept)
    r = run("healthchecks.zsh", "--config", str(dept.path), "--ping", "nope", env={"HEALTHCHECKS_PING_KEY": PING_KEY})
    assert r.returncode != 0 and "no check 'nope'" in r.stderr


def test_ping_failure_warns_but_never_fails_the_caller(run, dept, shims):
    enable_hc(dept)
    shims.on("curl", r"hc-ping\.com", exit=7)
    r = run("healthchecks.zsh", "--config", str(dept.path), "--ping", "orfe-chat-updates",
            env={"HEALTHCHECKS_PING_KEY": PING_KEY})
    assert r.returncode == 0 and "ping to orfe-chat-updates failed" in r.stderr


def test_delete_needs_typed_confirmation(run, dept, shims):
    enable_hc(dept)
    r = run("healthchecks.zsh", "--config", str(dept.path), "--delete", "--server", "lab",
            env={"HEALTHCHECKS_API_KEY": API_KEY})
    assert r.returncode != 0 and "DELETE-CHECKS lab" in r.stderr
    assert not any("DELETE" in c["args"] for c in shims.calls("curl"))


def test_delete_server_checks(run, dept, shims):
    enable_hc(dept)
    shims.on("curl", r"GET .*slug=orfe-chat-lab-", {"checks": [{"uuid": "u-9", "update_url": "https://healthchecks.io/api/v3/checks/u-9"}]})
    shims.on("curl", r"-X DELETE ", {})
    r = run("healthchecks.zsh", "--config", str(dept.path), "--delete", "--server", "lab",
            env={"HEALTHCHECKS_API_KEY": API_KEY, "CHAT_CONFIRM": "DELETE-CHECKS lab"})
    assert r.returncode == 0, r.stderr
    deletes = [c["args"][-1] for c in shims.calls("curl") if "DELETE" in c["args"]]
    assert deletes == ["https://healthchecks.io/api/v3/checks/u-9"] * 3  # health, web, ip-gate


def test_deploy_requires_the_ping_key_when_enabled(run, dept, shims):
    enable_hc(dept)
    deploy_rules(shims, missing=("healthchecks-ping-key",))
    r = run("deploy-server.zsh", "--config", str(dept.path), "--server", "dept", "--image", IMAGE)
    assert r.returncode != 0 and "lacks healthchecks-ping-key" in r.stderr


def test_deploy_with_healthchecks_deploys_the_pinger(run, dept, shims):
    enable_hc(dept)
    shims.on("az", r"^keyvault secret show .*healthchecks-api-key --query id", exit=3)
    deploy_rules(shims)
    r = run("deploy-server.zsh", "--config", str(dept.path), "--server", "dept", "--image", IMAGE)
    assert r.returncode == 0, r.stderr
    app_call = next(c for c in shims.calls("az") if "chat-dept-app" in " ".join(c["args"]))
    params = json.loads(next(iter(app_call["at_files"].values())))["parameters"]
    assert params["healthchecksEnabled"]["value"] is True
    assert "orfe-chat-dept-hc pings orfe-chat-dept-health every 5 min" in r.stderr
    assert "no healthchecks-api-key" in r.stderr


# ---------------------------------------------------------------- keepalive.zsh


def keepalive_rules(sh, *, base, expires, smoke_ok=True):
    az_basics(sh)
    sh.on("az", r"^containerapp env show .*defaultDomain", DOMAIN)
    sh.on("az", r"^keyvault secret show .*dept-oidc-secret --query attributes\.expires", expires)
    sh.on("curl", r"hc-ping\.com", "OK")
    if smoke_ok:
        sh.on("curl", rf"{base}/api/v1/server_settings", {"result": "success", "realm_url": base})
    else:
        sh.on("curl", rf"{base}/api/v1/server_settings", exit=22)
    sh.on("curl", r"url_effective", "https://login.microsoftonline.com/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/x")
    sh.on("curl", r"/ahmadi-group$", f"302 https://orfe-chat-groups.{DOMAIN}/")
    sh.on("curl", r"/lab$", f"302 https://orfe-chat-lab.{DOMAIN}/")


def in_days(n):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=n, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_keepalive_all_good_pings_success(run, dept, shims):
    enable_hc(dept)
    keepalive_rules(shims, base=f"https://orfe-chat-dept.{DOMAIN}", expires=in_days(300))
    r = run("keepalive.zsh", "--config", str(dept.path), "--server", "dept", env={"HEALTHCHECKS_PING_KEY": PING_KEY})
    assert r.returncode == 0, r.stderr
    ping = next(c for c in shims.calls("curl") if "hc-ping.com" in c.get("stdin", ""))
    assert f"https://hc-ping.com/{PING_KEY}/orfe-chat-dept-web?create=1" in ping["stdin"]


def test_keepalive_smoke_failure_pings_fail_and_exits_nonzero(run, dept, shims):
    enable_hc(dept)
    keepalive_rules(shims, base=f"https://orfe-chat-dept.{DOMAIN}", expires=in_days(300), smoke_ok=False)
    r = run("keepalive.zsh", "--config", str(dept.path), "--server", "dept", env={"HEALTHCHECKS_PING_KEY": PING_KEY})
    assert r.returncode != 0 and "keepalive failed for: dept" in r.stderr
    ping = next(c for c in shims.calls("curl") if "hc-ping.com" in c.get("stdin", ""))
    assert "/orfe-chat-dept-web/fail?create=1" in ping["stdin"]
    assert "smoke test failed" in ping["args"][ping["args"].index("--data-raw") + 1]


def test_keepalive_expiring_secret_and_certificate(run, dept, shims):
    dept.edit(lambda c: c["servers"]["dept"].update(dns="live"))
    dept.render()
    keepalive_rules(shims, base="https://chat.orfe.example.edu", expires=in_days(10))
    r = run("keepalive.zsh", "--config", str(dept.path), "--server", "dept",
            env={"CHAT_PROBE_FIXTURE": json.dumps({"chat.orfe.example.edu": 9})})
    assert r.returncode != 0
    assert "TLS certificate for chat.orfe.example.edu expires in 9 days" in r.stderr
    assert "Entra client secret expires in 10 days" in r.stderr


def test_keepalive_warns_early(run, dept, shims):
    dept.edit(lambda c: c["servers"]["dept"].update(dns="live"))
    dept.render()
    keepalive_rules(shims, base="https://chat.orfe.example.edu", expires=in_days(45))
    r = run("keepalive.zsh", "--config", str(dept.path), "--server", "dept",
            env={"CHAT_PROBE_FIXTURE": json.dumps({"chat.orfe.example.edu": 25})})
    assert r.returncode == 0, r.stderr
    assert "expires in 25 days" in r.stderr and "Entra client secret expires in 45 days" in r.stderr


# ---------------------------------------------------------------- teardown.zsh


def test_teardown_needs_the_department_phrase(run, dept, shims):
    r = run("teardown.zsh", "--config", str(dept.path), env={"CHAT_CONFIRM": "TEARDOWN other"})
    assert r.returncode != 0 and "did not match 'TEARDOWN orfe'" in r.stderr
    assert shims.calls("az") == []


def test_purge_phrase_is_different(run, dept, shims):
    r = run("teardown.zsh", "--config", str(dept.path), "--purge", env={"CHAT_CONFIRM": "TEARDOWN orfe"})
    assert r.returncode != 0 and "'TEARDOWN orfe PURGE'" in r.stderr


def test_platform_requires_purge_and_every_server(run, dept):
    r = run("teardown.zsh", "--config", str(dept.path), "--platform", env={"CHAT_CONFIRM": "x"})
    assert r.returncode != 0 and "needs --purge" in r.stderr
    r = run("teardown.zsh", "--config", str(dept.path), "--platform", "--purge", "--server", "lab", env={"CHAT_CONFIRM": "x"})
    assert r.returncode != 0 and "needs every server" in r.stderr
    r = run("teardown.zsh", "--config", str(dept.path), "--github", env={"CHAT_CONFIRM": "x"})
    assert r.returncode != 0 and "only together with --platform" in r.stderr


def test_preserve_teardown_runs_each_server_and_leaves_entra(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-\w+ --query id", "/app")
    shims.on("az", r"^containerapp delete ")
    shims.on("az", r"^containerapp job show ", exit=3)
    shims.on("az", r"^ad app list ", "")
    r = run("teardown.zsh", "--config", str(dept.path), env={"CHAT_CONFIRM": "TEARDOWN orfe"})
    assert r.returncode == 0, r.stderr
    deleted = [c.split(" -n ")[1].split()[0] for c in shims.joined("az") if c.startswith("containerapp delete")]
    assert deleted == ["orfe-chat-dept", "orfe-chat-groups", "orfe-chat-lab"]
    assert not any(c.startswith("ad app delete") for c in shims.joined("az"))
    assert "platform (orfe-chat-rg) is still there" in r.stderr


def test_entra_flag_deletes_sign_in_apps(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show ", exit=3)
    shims.on("az", r"^containerapp job show ", exit=3)
    shims.on("az", r"^ad app list --display-name orfe-chat-lab-zulip", "44444444-4444-4444-4444-444444444444")
    shims.on("az", r"^ad app list ", "")
    shims.on("az", r"^ad app delete ")
    r = run("teardown.zsh", "--config", str(dept.path), "--server", "lab", "--entra", env={"CHAT_CONFIRM": "TEARDOWN orfe"})
    assert r.returncode == 0, r.stderr
    assert [c for c in shims.joined("az") if c.startswith("ad app delete")] == \
        ["ad app delete --id 44444444-4444-4444-4444-444444444444"]
    assert [c for c in shims.joined("az") if c.startswith("containerapp delete")] == []


def test_unknown_flag_rejected_before_anything(run, dept, shims):
    r = run("teardown.zsh", "--config", str(dept.path), "--everything", env={"CHAT_CONFIRM": "TEARDOWN orfe"})
    assert r.returncode != 0 and "unknown argument: --everything" in r.stderr
    assert shims.calls() == []


# ---------------------------------------------------------------- grant-access.zsh


def grant_rules(sh, *, missing_secrets=(), existing_identity=False, granted=()):
    az_basics(sh)
    sh.on("az", r"^acr show -n orfechatacr -g orfe-chat-rg --query id", "/subs/x/registries/orfechatacr")
    sh.on("az", r"^keyvault show -n orfe-chat-kv ", "/subs/x/vaults/orfe-chat-kv")
    sh.on("az", r"^keyvault secret show-deleted ", exit=3)
    for s in missing_secrets:
        sh.on("az", rf"^keyvault secret show --vault-name orfe-chat-kv --name {s} --query id", exit=3, times=1)
    sh.on("az", r"^keyvault secret show --vault-name orfe-chat-kv --name \S+ --query id", "id")
    sh.on("az", r"^keyvault secret set ")
    if existing_identity:
        sh.on("az", r"^identity show -g orfe-chat-rg -n \S+ --query principalId", "pid-9")
        sh.on("az", r"^identity show ", "{}")
    else:
        sh.on("az", r"^identity show -g orfe-chat-rg -n \S+ --query principalId", "pid-9")
        sh.on("az", r"^identity show -g orfe-chat-rg -n \S+$", exit=3)
    sh.on("az", r"^identity create ")
    for s in granted:
        sh.on("az", rf"^role assignment list .*/secrets/{s} ", "/ra/x")
    if granted:
        sh.on("az", r"^role assignment list .*--scope /subs/x/registries/orfechatacr --role AcrPull", "/ra/acr")
    sh.on("az", r"^role assignment list ", "")
    sh.on("az", r"^role assignment create ")


def test_grant_access_scopes_each_identity_to_its_own_secrets(run, dept, shims):
    grant_rules(shims, missing_secrets=("dept-redis-password",))
    r = run("grant-access.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode == 0, r.stderr
    calls = shims.joined("az")
    assert [c for c in calls if c.startswith("identity create")] == [
        "identity create -g orfe-chat-rg -n orfe-chat-dept-id -l canadacentral --tags chat-server=dept --output none",
        "identity create -g orfe-chat-rg -n orfe-chat-dept-db-id -l canadacentral --tags chat-server=dept --output none"]
    scopes = [c.split("--scope ")[1].split()[0] for c in calls if c.startswith("role assignment create")]
    acr = [c for c in calls if c.startswith("role assignment create") and "--role AcrPull --scope /subs/x/registries/orfechatacr" in c]
    assert len(acr) == 2  # both identities pull from the department registry
    scopes = [x for x in scopes if "/registries/" not in x]
    base = "/subs/x/vaults/orfe-chat-kv/secrets/"
    assert scopes == [base + s for s in sorted([
        "dept-memcached-password", "dept-oidc-secret", "dept-postgres-password", "dept-rabbitmq-password",
        "dept-redis-password", "dept-secret-key", "email-password"])] + [base + "dept-postgres-password", base + "pg-admin-password"]
    assert base + "pg-admin-password" not in scopes[:7]  # the app identity never reads the PG admin password
    assert not any("/vaults/orfe-chat-kv " in c or c.endswith("/vaults/orfe-chat-kv") for c in calls if "role assignment create" in c)
    gen = next(c for c in shims.calls("az") if c["args"][:3] == ["keyvault", "secret", "set"])
    assert len(gen["file"]) == 64 and "\n" not in gen["file"]  # no trailing newline stored
    assert not any(gen["file"] in a for c in shims.calls() for a in c["args"])


def test_grant_access_is_idempotent(run, dept, shims):
    grant_rules(shims, existing_identity=True, granted=[
        "dept-memcached-password", "dept-oidc-secret", "dept-postgres-password", "dept-rabbitmq-password",
        "dept-redis-password", "dept-secret-key", "email-password", "pg-admin-password"])
    r = run("grant-access.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith(("identity create", "role assignment create", "keyvault secret set")) for c in shims.joined("az"))


def test_grant_access_needs_operator_secrets_first(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^keyvault secret show --vault-name orfe-chat-kv --name dept-oidc-secret --query id", exit=3)
    grant_rules(shims)
    r = run("grant-access.zsh", "--config", str(dept.path), "--server", "dept")
    assert r.returncode != 0 and "create these first: dept-oidc-secret" in r.stderr
    assert not any(c.startswith("role assignment create") for c in shims.joined("az"))


# ---------------------------------------------------------------- acs-email.zsh


def acs_dept(dept, domain="orfe.example.edu"):
    email = {"provider": "acs", "acs": {"domain": domain}}
    if domain != "azure-managed":
        email["from"] = f"donotreply@{domain}"
    dept.edit(lambda c: c.update(email=email))
    dept.render()


def acs_rules(sh, *, states, linked="[]"):
    az_basics(sh)
    sh.on("az", r"^communication show -g orfe-chat-rg -n orfe-chat-acs --query id", "/subs/x/acs")
    sh.on("az", r"^communication show .*linkedDomains", linked)
    sh.on("az", r"^communication email domain show .* --query id", "/subs/x/email/domains/orfe.example.edu")
    sh.on("az", r"^communication email domain show ", {
        "mailFromSenderDomain": "1234abcd.azurecomm.net",
        "verificationStates": states,
        "verificationRecords": {"Domain": {"type": "TXT", "name": "orfe.example.edu", "value": "ms-domain-verification=abc", "ttl": 3600},
                                "SPF": {"type": "TXT", "name": "orfe.example.edu", "value": "v=spf1 include:spf.protection.outlook.com -all", "ttl": 3600},
                                "DKIM": {"type": "CNAME", "name": "selector1-azurecomm-prod-net._domainkey", "value": "selector1.example", "ttl": 3600}}})
    sh.on("az", r"^communication email domain initiate-verification ")
    sh.on("az", r"^communication update ")
    sh.on("az", r"^ad app list --display-name orfe-chat-acs-smtp", "55555555-5555-5555-5555-555555555555")
    sh.on("az", r"^ad sp show --id \S+ --query id", "sp-acs")
    sh.on("az", r"^ad sp show ", "{}")
    sh.on("az", r"^role assignment list ", "")
    sh.on("az", r"^role assignment create ")
    sh.on("az", r"^keyvault secret show .*email-password --query id", exit=3, times=1)
    sh.on("az", r"^keyvault secret show-deleted ", exit=3)
    sh.on("az", r"^ad app credential reset ", {"password": "ACS-SECRET-1", "end": "2028-09-30T00:00:00Z"})
    sh.on("az", r"^keyvault secret set ")
    sh.on("az", r"^communication smtp-username show ", exit=3)
    sh.on("az", r"^communication smtp-username create ")


V = {"Domain": {"status": "Verified"}, "SPF": {"status": "Verified"}, "DKIM": {"status": "Verified"},
     "DKIM2": {"status": "Verified"}, "DMARC": {"status": "NotStarted"}}


def test_acs_print_writes_the_dns_ticket(run, dept, shims):
    acs_dept(dept)
    acs_rules(shims, states={})
    r = run("acs-email.zsh", "--config", str(dept.path), "--print")
    assert r.returncode == 0, r.stderr
    assert "| TXT | `orfe.example.edu` | `ms-domain-verification=abc` | 3600 |" in r.stdout
    assert "| CNAME | `selector1-azurecomm-prod-net._domainkey` |" in r.stdout
    assert "_dmarc.orfe.example.edu" in r.stdout
    assert not any(c.startswith(("ad app", "role assignment")) for c in shims.joined("az"))


def test_acs_unverified_domain_is_not_linked_but_credentials_are_made(run, dept, shims):
    acs_dept(dept)
    acs_rules(shims, states={**V, "DKIM2": {"status": "NotStarted"}})
    r = run("acs-email.zsh", "--config", str(dept.path))
    assert r.returncode == 0, r.stderr
    assert "not verified yet (DKIM2)" in r.stderr
    calls = shims.joined("az")
    assert not any(c.startswith("communication update") for c in calls)
    assert any("--role Communication and Email Service Owner --scope /subs/x/acs" in c for c in calls)
    assert any(c.startswith("communication smtp-username create -g orfe-chat-rg --comm-service-name orfe-chat-acs -n orfe-chat-smtp "
                            "--username orfe-chat-smtp --entra-application-id 55555555-5555-5555-5555-555555555555") for c in calls)
    secret = next(c for c in shims.calls("az") if c["args"][:3] == ["keyvault", "secret", "set"])
    assert secret["file"] == "ACS-SECRET-1" and "--expires" in secret["args"]
    assert not any("ACS-SECRET-1" in a for c in shims.calls() for a in c["args"])


def test_acs_verified_domain_gets_linked(run, dept, shims):
    acs_dept(dept)
    acs_rules(shims, states=V)
    r = run("acs-email.zsh", "--config", str(dept.path))
    assert r.returncode == 0, r.stderr
    assert 'communication update -g orfe-chat-rg -n orfe-chat-acs --linked-domains ["/subs/x/email/domains/orfe.example.edu"] --output none' in shims.joined("az")


def test_acs_refuses_without_acs_provider(run, dept, shims):
    r = run("acs-email.zsh", "--config", str(dept.path))
    assert r.returncode != 0 and "email.provider is not acs" in r.stderr


# ---------------------------------------------------------------- build-image.zsh


def build_rules(sh, *, built=False, sidecars_present=False):
    az_basics(sh)
    sh.on("az", r"^acr show -n orfechatacr -g orfe-chat-rg --query loginServer", "orfechatacr.azurecr.io")
    if built:
        sh.on("az", r"^acr repository show -n orfechatacr --image chat:", "sha256:" + "f" * 64)
    else:
        sh.on("az", r"^acr repository show -n orfechatacr --image chat:", exit=3, times=1)
        sh.on("az", r"^acr repository show -n orfechatacr --image chat:", "sha256:" + "f" * 64)
    sh.on("az", r"^acr repository show ", "sha256:" + "1" * 64 if sidecars_present else "", exit=0 if sidecars_present else 3)
    sh.on("az", r"^acr build ")
    sh.on("az", r"^acr import ")
    sh.on("az", r"^acr repository update ")


def template_at(tmp_path, sha, ref="v0.3.0"):
    lock = tmp_path / "template.lock"
    lock.write_text(json.dumps({"repo": "pu-shd/chat", "ref": ref, "sha": sha}))
    return lock


def fake_template(tmp_path):
    """A git checkout of the template's image/ dir, so build-image.zsh can check its commit."""
    import shutil
    root = tmp_path / "tmpl"
    shutil.copytree(ROOT / "image", root / "image")
    shutil.copytree(ROOT / "scripts", root / "scripts")
    shutil.copytree(ROOT / "tools", root / "tools")
    shutil.copytree(ROOT / "schema", root / "schema")
    g = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q"); g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@example.edu", "commit", "-qm", "t")
    return root, g("rev-parse", "HEAD")


def test_build_image_builds_once_locks_and_imports_sidecars(run, dept, shims, tmp_path):
    root, sha = fake_template(tmp_path)
    build_rules(shims)
    r = run_in(root, run, "build-image.zsh", "--config", str(dept.path), "--lock", str(template_at(tmp_path, sha)))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "orfechatacr.azurecr.io/chat@sha256:" + "f" * 64
    calls = shims.joined("az")
    build = [c for c in calls if c.startswith("acr build")]
    assert len(build) == 1 and f"-t chat:v0.3.0-{sha[:7]} --platform linux/amd64 --build-arg CHAT_TEMPLATE_SHA={sha}" in build[0]
    imports = [c for c in calls if c.startswith("acr import")]
    assert any("--source docker.io/library/redis:" in c and "--image library/redis:" in c for c in imports)
    assert any("--source docker.io/curlimages/curl:" in c for c in imports) and len(imports) == 5
    locks = [c for c in calls if c.startswith("acr repository update")]
    assert all("--write-enabled false --delete-enabled false" in c for c in locks) and len(locks) == 6


def test_build_image_reuses_an_existing_tag(run, dept, shims, tmp_path):
    root, sha = fake_template(tmp_path)
    build_rules(shims, built=True, sidecars_present=True)
    r = run_in(root, run, "build-image.zsh", "--config", str(dept.path), "--lock", str(template_at(tmp_path, sha)))
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith(("acr build", "acr import")) for c in shims.joined("az"))


def test_build_image_refuses_a_checkout_that_is_not_the_locked_commit(run, dept, shims, tmp_path):
    root, sha = fake_template(tmp_path)
    build_rules(shims)
    r = run_in(root, run, "build-image.zsh", "--config", str(dept.path), "--lock", str(template_at(tmp_path, "0" * 40)))
    assert r.returncode != 0 and "template.lock pins 0000000" in r.stderr
    (root / "image" / "Dockerfile").write_text("FROM evil\n")
    r = run_in(root, run, "build-image.zsh", "--config", str(dept.path), "--lock", str(template_at(tmp_path, sha)))
    assert r.returncode != 0 and "with changes in image/" in r.stderr
    assert not any(c.startswith("acr build") for c in shims.joined("az"))


def run_in(root, run, script, *args):
    """Run a script from a different template checkout (CHAT_ROOT follows the script path)."""
    import os
    env = {"CHAT_ROOT": str(root)}
    return run(script, *args, env=env)


# ---------------------------------------------------------------- realm.zsh --set-role


def role_rules(sh, result):
    az_basics(sh)
    sh.on("az", r"^containerapp job start --name orfe-chat-dept-mgmt ", "exec-role")
    sh.on("az", r"^containerapp job execution show ", "Succeeded")
    sh.on("az", r"^containerapp job logs show ", "CHAT-RESULT: " + json.dumps(result))


def test_set_role_owner_needs_typed_confirmation(run, dept, shims):
    role_rules(shims, {"ok": True, "email": "new@example.edu", "role": "owner", "created": True})
    r = run("realm.zsh", "--config", str(dept.path), "--server", "dept", "--set-role", "_root", "new@example.edu", "owner", "New Owner")
    assert r.returncode != 0 and "GRANT owner new@example.edu" in r.stderr
    assert not any("job start" in c for c in shims.joined("az"))
    r = run("realm.zsh", "--config", str(dept.path), "--server", "dept", "--set-role", "_root", "new@example.edu", "owner",
            "New Owner", env={"CHAT_CONFIRM": "GRANT owner new@example.edu"})
    assert r.returncode == 0, r.stderr
    start = next(c for c in shims.joined("az") if "job start" in c)
    assert "--args chat:manage set-role _root new@example.edu owner New Owner" in start


def test_set_role_moderator_needs_no_confirmation_and_checks_the_result(run, dept, shims):
    role_rules(shims, {"ok": True, "email": "someone-else@example.edu", "role": "moderator", "created": False})
    r = run("realm.zsh", "--config", str(dept.path), "--server", "dept", "--set-role", "_root", "m@example.edu", "moderator")
    assert r.returncode != 0 and "does not satisfy" in r.stderr  # the job answered about someone else


@pytest.mark.parametrize("email, role", [("not-an-email", "member"), ("a@example.edu", "superuser")])
def test_set_role_rejects_bad_input(run, dept, shims, email, role):
    az_basics(shims)
    r = run("realm.zsh", "--config", str(dept.path), "--server", "dept", "--set-role", "_root", email, role)
    assert r.returncode != 0
    assert not any("job start" in c for c in shims.joined("az"))
