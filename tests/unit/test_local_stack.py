"""tools/local_stack.py and scripts/local.zsh: a department's chat.yml run on this machine."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from conftest import FIXTURES, ROOT

import local_stack  # tools/ is on sys.path via conftest
import render


def fixture_cfg() -> dict:
    return yaml.safe_load((FIXTURES / "orfe" / "chat.yml").read_text())


def localize(tmp_path, *args: str) -> tuple[subprocess.CompletedProcess, dict | None]:
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "local_stack.py"), "localize",
                        str(FIXTURES / "orfe"), str(tmp_path / "out"), *args],
                       capture_output=True, text=True)
    return r, (json.loads(r.stdout) if r.returncode == 0 else None)


def resolve(server_json: str) -> dict:
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "render.py"), "resolve", "--server", server_json,
                          "--default-domain", "local.invalid", "--image", "pu-shd-chat:e2e",
                          "--registry", "localchatacr.azurecr.io", "--allow-unpinned-image"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ---------------------------------------------------------------- localize


def test_localize_suffixes_every_host_and_drops_cloud_only_features():
    cfg, server = local_stack.localize_config(fixture_cfg(), None, None)
    assert server == "dept"
    s = cfg["servers"]
    assert s["dept"]["host"] == "chat.orfe.example.edu.localhost"
    assert s["groups"]["external_host"] == "groups.chat.orfe.example.edu.localhost"
    assert s["groups"]["realm_domain"] == "chat.orfe.example.edu.localhost"
    assert "ip_gate" not in s["lab"] and "cert" not in s["lab"]
    assert cfg["defaults"]["dns"] == "live"
    assert cfg["ip_gate"] == {"enabled": False} and cfg["healthchecks"] == {"enabled": False}
    assert all(v["entra"]["client_id"] == local_stack.CLIENT_ID for v in s.values())


def test_localize_does_not_touch_the_input():
    cfg = fixture_cfg()
    local_stack.localize_config(cfg, "groups", "paper-tiger")
    assert cfg == fixture_cfg()


def test_localize_rejects_an_unknown_server():
    with pytest.raises(local_stack.LocalError, match="no server 'nope'.*dept, groups, lab"):
        local_stack.localize_config(fixture_cfg(), "nope", None)


def test_theme_override_applies_to_the_server_and_its_realms():
    cfg = fixture_cfg()
    cfg["servers"]["groups"]["realms"][0]["theme"] = "paper-tiger"
    out, _ = local_stack.localize_config(cfg, "groups", "default")
    assert out["servers"]["groups"]["theme"] == "default"
    assert all("theme" not in r for r in out["servers"]["groups"]["realms"])


def test_localize_cli_renders_and_reports_what_it_serves(tmp_path):
    r, stack = localize(tmp_path, "--server", "groups")
    assert r.returncode == 0, r.stderr
    assert stack["server"] == "groups"
    assert stack["hosts"] == ["groups.chat.orfe.example.edu.localhost", "auth.groups.chat.orfe.example.edu.localhost",
                              "ahmadi-group.chat.orfe.example.edu.localhost", "beta-lab.chat.orfe.example.edu.localhost"]
    assert [x["url"] for x in stack["realms"]] == ["https://ahmadi-group.chat.orfe.example.edu.localhost/",
                                                   "https://beta-lab.chat.orfe.example.edu.localhost/"]
    assert stack["realms"][0]["owner"] == {"email": "pi@example.edu", "name": "PI"}
    # A gated server with a Key Vault certificate still runs locally.
    r, stack = localize(tmp_path, "--server", "lab")
    assert r.returncode == 0, r.stderr
    assert stack["hosts"] == ["lab.chat.orfe.example.edu.localhost"]


def test_localize_cli_fails_loudly_on_an_unknown_theme(tmp_path):
    r, _ = localize(tmp_path, "--theme", "no-such-theme")
    assert r.returncode == 2
    assert "does not render" in r.stderr and "no-such-theme" in r.stderr


# ---------------------------------------------------------------- override


@pytest.fixture
def dept_local(tmp_path):
    r, stack = localize(tmp_path, "--theme", "paper-tiger")
    assert r.returncode == 0, r.stderr
    spec = json.loads(open(stack["server_json"]).read())
    return resolve(stack["server_json"]), spec


def test_override_points_everything_at_compose_services(dept_local):
    params, spec = dept_local
    o = local_stack.build_override(params, spec, "172.30.0.0/16")["services"]
    env = o["zulip"]["environment"]
    assert env["SETTING_REMOTE_POSTGRES_HOST"] == "postgres"
    assert env["SETTING_REMOTE_POSTGRES_SSLMODE"] == "disable"
    assert (env["SETTING_REDIS_HOST"], env["SETTING_RABBITMQ_HOST"]) == ("redis", "rabbitmq")
    assert env["SETTING_MEMCACHED_LOCATION"] == "memcached:11211"
    assert (env["SETTING_EMAIL_HOST"], env["SETTING_EMAIL_PORT"], env["SETTING_EMAIL_USE_TLS"]) == ("mailpit", "1025", "False")
    assert env["LOADBALANCER_IPS"] == "172.30.0.0/16"
    assert env["CONFIG_http_proxy__allow_ranges"] == "172.30.0.0/16"
    assert env["CHAT_RATE_LIMIT_EXEMPT"].endswith(" 172.30.0.0/16")
    assert env["SETTING_EXTERNAL_HOST"] == "chat.orfe.example.edu.localhost"
    assert "chat.orfe.example.edu.localhost=paper-tiger" in env["CHAT_THEMES"].split()
    idps = env["SETTING_SOCIAL_AUTH_OIDC_ENABLED_IDPS"]
    assert f'"oidc_url": "{local_stack.IDP_ISSUER}"' in idps and local_stack.CLIENT_ID in idps
    # Nothing may still point at Azure or Entra.
    for v in env.values():
        assert "microsoftonline" not in v and "azure.com" not in v and "resend.com" not in v, v
    assert o["mgmt"]["environment"] == {**env, "AUTO_BACKUP_ENABLED": "False"}
    assert o["dbinit"]["environment"] == {"DB_NAME": "zulip_dept", "DB_USER": "zulip_dept"}


def test_mock_entra_signs_in_the_realm_owner_for_this_client(dept_local):
    params, spec = dept_local
    oidc = local_stack.build_override(params, spec, "172.30.0.0/16")["services"]["oidc"]["environment"]
    assert oidc["SERVER_PORT"] == str(local_stack.IDP_PORT)
    claims = json.loads(oidc["JSON_CONFIG"])["tokenCallbacks"][0]["requestMappings"][0]["claims"]
    assert claims["email"] == "admin@example.edu" and claims["name"] == "Admin"
    assert claims["aud"] == [local_stack.CLIENT_ID]


def test_override_refuses_settings_it_does_not_recognise(dept_local):
    params, spec = dept_local
    params["parameters"]["zulipEnv"]["value"] = [e for e in params["parameters"]["zulipEnv"]["value"]
                                                 if e["name"] != "SETTING_REDIS_HOST"]
    with pytest.raises(local_stack.LocalError, match="SETTING_REDIS_HOST"):
        local_stack.build_override(params, spec, "172.30.0.0/16")


def test_caddyfile_serves_only_localhost_names():
    text = local_stack.caddyfile(["a.localhost", "b.a.localhost"])
    assert "a.localhost, b.a.localhost {" in text and "reverse_proxy zulip:80" in text
    assert "local_certs" in text
    for bad in (["chat.orfe.princeton.edu"], ["x.localhost {\n}"]):
        with pytest.raises(local_stack.LocalError):
            local_stack.caddyfile(bad)


# ---------------------------------------------------------------- local.zsh


SUBNET = "172.30.0.0/16"
OK_REALM = 'CHAT-RESULT: {"ok": true, "created": true, "slug": "", "url": "https://chat.orfe.example.edu.localhost"}'


def local_rules(shims):
    shims.on("lsof", r"", exit=1)  # nothing listening
    shims.on("docker", r"^network inspect chat-local_e2e", exit=1, times=1)  # no network yet
    shims.on("docker", r"^network inspect chat-local_e2e", SUBNET)
    shims.on("docker", r"^inspect -f \{\{\.State\.Health\.Status\}\} cid-1$", "healthy")
    shims.on("docker-compose", r" ps -q zulip$", "cid-1")
    shims.on("docker-compose", r" run --rm -T mgmt chat:manage ensure-realm ", OK_REALM)
    shims.on("docker-compose", r" (build zulip|up --no-start zulip edge|up -d zulip edge mailpit)$")


def test_local_up_renders_starts_and_creates_the_realm(run, shims, tmp_path):
    local_rules(shims)
    state = tmp_path / "state"
    r = run("local.zsh", "up", "--config", str(FIXTURES / "orfe"), env={"CHAT_LOCAL_STATE": str(state)})
    assert r.returncode == 0, r.stderr
    calls = shims.joined("docker-compose")
    base = f"-p chat-local -f {ROOT}/tests/e2e/docker-compose.yml -f {ROOT}/tests/e2e/local.yml -f {state}/override.json"
    assert all(c.startswith(base) for c in calls), calls
    assert calls.index(f"{base} up --no-start zulip edge") < calls.index(f"{base} up -d zulip edge mailpit")
    assert f"{base} run --rm -T mgmt chat:manage ensure-realm _root ORFE admin@example.edu Admin" in calls
    override = json.loads((state / "override.json").read_text())
    assert override["services"]["zulip"]["environment"]["LOADBALANCER_IPS"] == SUBNET  # the real subnet, not the placeholder
    assert "chat.orfe.example.edu.localhost {" in (state / "Caddyfile").read_text()
    for s in ("zulip__secret_key", "zulip__postgres_password", "pg_admin_password"):
        assert (state / "secrets" / s).stat().st_size > 0
    assert "ORFE: https://chat.orfe.example.edu.localhost/" in r.stderr


def test_local_up_fails_when_a_port_is_taken(run, shims, tmp_path):
    shims.on("lsof", r"-iTCP:443 ", "nginx 1 root")
    r = run("local.zsh", "up", "--config", str(FIXTURES / "orfe"), env={"CHAT_LOCAL_STATE": str(tmp_path / "s")})
    assert r.returncode != 0
    assert "port 443 is in use" in r.stderr
    assert shims.calls("docker-compose") == []


def test_local_up_fails_before_docker_on_an_unknown_server(run, shims, tmp_path):
    shims.on("lsof", r"", exit=1)
    r = run("local.zsh", "up", "--config", str(FIXTURES / "orfe"), "--server", "nope",
            env={"CHAT_LOCAL_STATE": str(tmp_path / "s")})
    assert r.returncode != 0
    assert "no server 'nope'" in r.stderr
    assert shims.calls("docker-compose") == [] and shims.calls("docker") == []


def test_local_up_fails_when_the_realm_job_does_not_report_ok(run, shims, tmp_path):
    shims.on("docker-compose", r" run --rm -T mgmt ", 'CHAT-RESULT: {"ok": false, "error": "boom"}')
    local_rules(shims)
    r = run("local.zsh", "up", "--config", str(FIXTURES / "orfe"), env={"CHAT_LOCAL_STATE": str(tmp_path / "s")})
    assert r.returncode != 0
    assert "ensure-realm _root did not report ok" in r.stderr


@pytest.mark.parametrize("args, message", [
    ([], "usage:"),
    (["restart"], "unknown action: restart"),
    (["down", "--server", "dept"], "only apply to up"),
    (["status"], "not running"),
])
def test_local_usage_errors(run, tmp_path, args, message):
    r = run("local.zsh", *args, env={"CHAT_LOCAL_STATE": str(tmp_path / "s")})
    assert r.returncode != 0
    assert message in r.stderr


def test_local_down_removes_the_stack_and_its_state(run, shims, tmp_path):
    state = tmp_path / "s"
    (state / "secrets").mkdir(parents=True)
    (state / "override.json").write_text("{}")
    shims.on("docker-compose", r" --profile jobs down -v --remove-orphans$")
    r = run("local.zsh", "down", env={"CHAT_LOCAL_STATE": str(state)})
    assert r.returncode == 0, r.stderr
    assert len(shims.calls("docker-compose")) == 1
    assert not state.exists()
