"""tools/render.py: regression tests for the audit findings (settings injection, derived-name
collisions, string hygiene, Zulip limits, Azure naming)."""
from __future__ import annotations

import json

import pytest

import render

DOMAIN = "happy-sea-123.canadacentral.azurecontainerapps.io"
REGISTRY = "orfechatacr.azurecr.io"
IMAGE = f"{REGISTRY}/chat@sha256:" + "a" * 64


def rerender_error(dept, capsys) -> str:
    capsys.readouterr()
    assert render.main(["render", str(dept.path)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR: "), err
    return err


def resolve(dept, server, capsys) -> dict:
    capsys.readouterr()
    code = render.main([
        "resolve", "--server", str(dept.path / "generated" / "servers" / f"{server}.json"),
        "--default-domain", DOMAIN, "--image", IMAGE, "--registry", REGISTRY,
    ])
    out = capsys.readouterr()
    assert code == 0, out.err
    params = json.loads(out.out)["parameters"]
    params["_env"] = {e["name"]: e["value"] for e in params["zulipEnv"]["value"]}
    return params


def add_server(name: str, **extra):
    def fn(c):
        c["servers"][name] = {"kind": "dedicated", "host": f"{name}.other.example.edu", "slug": f"{name}-grp",
                              "realm": {"name": name.upper(), "owner": {"email": "o@example.edu", "name": "O"}},
                              **extra}
    return fn


# ---------------------------------------------------------------- 1. from_name / SETTING_* injection


def test_from_name_is_not_sent_to_zulip_and_warns(dept, capsys):
    dept.edit(lambda c: c["email"].update(from_name="ORFE Chat (x)"))
    capsys.readouterr()
    dept.render()
    err = capsys.readouterr().err
    assert "WARNING: email.from_name has no effect on Zulip's mail" in err
    for name in ("dept", "groups", "lab"):
        assert "SETTING_DEFAULT_FROM_EMAIL" not in dept.server(name)["settings"]
    env = resolve(dept, "dept", capsys)["_env"]
    assert "SETTING_DEFAULT_FROM_EMAIL" not in env
    assert not any("ORFE Chat (x)" in v for v in env.values())
    # deploy-platform.zsh still reads it for ACS sender display names.
    plat = json.loads((dept.path / "generated" / "platform.json").read_text())
    assert plat["email"]["from_name"] == "ORFE Chat (x)"


def test_no_from_name_warning_when_unset(dept, capsys):
    capsys.readouterr()
    dept.render()
    assert "from_name" not in capsys.readouterr().err


def test_audit_from_name_payload_is_rejected_by_the_schema(dept, capsys):
    dept.edit(lambda c: c["email"].update(from_name=" (__import__('os').system('id'))#\\x29\\c"))
    err = rerender_error(dept, capsys)
    assert "schema validation failed" in err and "email/from_name" in err


@pytest.mark.parametrize("value", ["a\\b", "\\x28", "Lab \\c"])
def test_schema_text_rejects_backslash(dept, capsys, value):
    dept.edit(lambda c: c["servers"]["dept"]["realm"].update(name=value))
    err = rerender_error(dept, capsys)
    assert "schema validation failed" in err and "servers/dept" in err


@pytest.mark.parametrize("value", [
    " (__import__('os').system('id'))",
    "(x)", "[1]", "{'a': 1}", "  [ 1 ]  ", " ( x", "{",
])
def test_check_setting_refuses_values_docker_zulip_would_paste_as_code(value):
    with pytest.raises(render.ConfigError, match=r"srv: SETTING_FOO would be read as Python code"):
        render.check_setting("srv", "SETTING_FOO", value)


@pytest.mark.parametrize("key", sorted(render.PYTHON_LITERAL_SETTINGS))
def test_check_setting_allows_the_intended_literals(key):
    render.check_setting("srv", key, '{"a": "b"}')


def test_python_literal_allowlist_is_exactly_what_render_emits():
    assert render.PYTHON_LITERAL_SETTINGS == {"SETTING_SOCIAL_AUTH_OIDC_ENABLED_IDPS", "SETTING_REALM_HOSTS"}


@pytest.mark.parametrize("key", ["SETTING_FOO", *sorted(render.PYTHON_LITERAL_SETTINGS)])
def test_check_setting_refuses_backslashes_everywhere(key):
    with pytest.raises(render.ConfigError, match=f"srv: {key} contains a backslash"):
        render.check_setting("srv", key, '{"a": "\\x28"}')


@pytest.mark.parametrize("value", ["a\nb", "a\tb", "a\x7fb", "\x00"])
def test_check_setting_refuses_control_characters(value):
    with pytest.raises(render.ConfigError, match="srv: SETTING_FOO contains a control character"):
        render.check_setting("srv", "SETTING_FOO", value)


@pytest.mark.parametrize("value", ["plain", "Name <a@b.edu>", "x (y)", "True", "5432", ""])
def test_check_setting_accepts_ordinary_values(value):
    render.check_setting("srv", "SETTING_FOO", value)


def test_non_setting_keys_may_look_like_literals():
    render.check_setting("srv", "CHAT_THEMES", "[x]")  # not written into settings.py


def test_every_rendered_setting_passes_check_setting(dept, capsys):
    for name in ("dept", "groups"):
        env = resolve(dept, name, capsys)["_env"]
        for k, v in env.items():
            render.check_setting(name, k, v)


# ---------------------------------------------------------------- 2. derived-name collisions


def test_db_identity_of_x_is_app_identity_of_x_db(dept, capsys):
    dept.edit(add_server("xx"))
    dept.edit(add_server("xx-db"))
    err = rerender_error(dept, capsys)
    assert "managed identity name 'orfe-chat-xx-db-id' is derived for both" in err
    assert "servers.xx (db identity)" in err and "servers.xx-db (app identity)" in err


def test_job_of_a_is_app_of_a_mgmt(dept, capsys):
    dept.edit(add_server("aa"))
    dept.edit(add_server("aa-mgmt"))
    err = rerender_error(dept, capsys)
    assert "container app/job name 'orfe-chat-aa-mgmt' is derived for both" in err
    assert "servers.aa (mgmt job)" in err and "servers.aa-mgmt (app)" in err


def test_hc_job_of_yy_is_app_of_yy_hc(dept, capsys):
    dept.edit(add_server("yy"))
    dept.edit(add_server("yy-hc"))
    err = rerender_error(dept, capsys)
    assert "container app/job name 'orfe-chat-yy-hc' is derived for both" in err


def test_check_unique_names_reports_hc_identity_collision():
    plat = {"prefix": "p", "names": {"identity": "p-id"}, "healthchecks": {"enabled": True}}

    def spec(name, **ident):
        return {"app_name": f"p-{name}", "jobs": {}, "identities": ident, "key_vault_secrets": {},
                "healthchecks": {"health": None, "web": None, "ip_gate": None},
                "database": {"name": f"zulip_{name}", "user": f"zulip_{name}"},
                "share": f"{name}-data", "env_storage": f"{name}-data"}
    servers = {"z": spec("z", hc={"name": "p-z-hc-id", "secrets": ["healthchecks-ping-key"]}),
               "zz": spec("zz", app={"name": "p-z-hc-id", "secrets": []})}
    with pytest.raises(render.ConfigError,
                       match=r"managed identity name 'p-z-hc-id' is derived for both servers.z \(hc identity\) "
                             r"and servers.zz \(app identity\)"):
        render.check_unique_names(plat, servers)


@pytest.mark.parametrize("kind, field, value, message", [
    ("Key Vault secret", "key_vault_secrets", {"x": "q-oidc-client-id"}, "Key Vault secret name 'q-oidc-client-id'"),
    ("database", "database", {"name": "zulip_q", "user": "zulip_q"}, "Postgres database/role name 'zulip_q'"),
    ("share", "share", "q-data", "storage share name 'q-data'"),
    ("env storage", "env_storage", "q-data", "environment storage name 'q-data'"),
    ("hc slug", "healthchecks", {"health": "p-q-health", "web": None, "ip_gate": None}, "Healthchecks slug name 'p-q-health'"),
])
def test_check_unique_names_covers_every_namespace(kind, field, value, message):
    plat = {"prefix": "p", "names": {"identity": "p-id"}, "healthchecks": {"enabled": True}}

    def spec(name):
        return {"app_name": f"p-{name}", "jobs": {}, "identities": {}, "key_vault_secrets": {},
                "healthchecks": {"health": f"p-{name}-health", "web": None, "ip_gate": None},
                "database": {"name": f"zulip_{name}", "user": f"zulip_{name}"},
                "share": f"{name}-data", "env_storage": f"{name}-data"}
    servers = {"q": spec("q"), "r": {**spec("r"), field: value}}
    with pytest.raises(render.ConfigError, match=f"{message} is derived for both servers.q .* and servers.r"):
        render.check_unique_names(plat, servers)


def test_shared_secrets_may_repeat_across_servers(dept):
    # The fixture rendered, although every server lists the department's email-password.
    assert all("email-password" in dept.server(n)["identities"]["app"]["secrets"] for n in ("dept", "groups", "lab"))


def test_hc_identity_only_with_healthchecks(dept, capsys):
    assert "hc" not in dept.server("dept")["identities"]
    assert resolve(dept, "dept", capsys)["hcIdentityName"]["value"] == ""
    dept.edit(lambda c: c.update(healthchecks={"enabled": True}))
    dept.render()
    for name in ("dept", "groups", "lab"):
        assert dept.server(name)["identities"]["hc"] == {
            "name": f"orfe-chat-{name}-hc-id", "secrets": ["healthchecks-ping-key"]}
    assert resolve(dept, "dept", capsys)["hcIdentityName"]["value"] == "orfe-chat-dept-hc-id"


def test_only_the_hc_identity_reads_the_ping_key(dept):
    dept.edit(lambda c: c.update(healthchecks={"enabled": True}))
    dept.render()
    for name in ("dept", "groups", "lab"):
        ids = dept.server(name)["identities"]
        holders = sorted(k for k, v in ids.items() if "healthchecks-ping-key" in v["secrets"])
        assert holders == ["hc"], (name, holders)


# ---------------------------------------------------------------- 3. string hygiene


@pytest.mark.parametrize("mutate, where", [
    (lambda c: c["servers"]["groups"]["realms"][0].update(slug="beta\n"), "servers/groups/realms/0/slug"),
    (lambda c: c.update(admin_email="admin@example.edu\n"), "admin_email"),
    (lambda c: c["servers"]["dept"].update(host="chat.orfe.example.edu\n"), "servers/dept/host"),
])
def test_trailing_newline_is_rejected(dept, capsys, mutate, where):
    dept.edit(mutate)
    err = rerender_error(dept, capsys)
    assert f"{where}: contains a control character" in err


@pytest.mark.parametrize("value", ["A {{external_host}}", "close }} here", "a {{"])
def test_placeholder_braces_rejected(dept, capsys, value):
    dept.edit(lambda c: c["servers"]["dept"]["realm"].update(name=value))
    err = rerender_error(dept, capsys)
    assert "servers/dept/realm/name: may not contain '{{' or '}}'" in err


def test_single_braces_still_allowed(dept):
    dept.edit(lambda c: c["servers"]["dept"]["realm"].update(name="ORFE {lab}"))
    dept.render()


# ---------------------------------------------------------------- 4-6. Zulip limits


def test_zulipinternal_is_reserved(dept, capsys):
    assert render.is_reserved("zulipinternal")
    dept.edit(lambda c: c["servers"]["groups"]["realms"][0].update(slug="zulipinternal"))
    assert "slug 'zulipinternal' is reserved by Zulip" in rerender_error(dept, capsys)


@pytest.mark.parametrize("slug", ["a", "ab"])
def test_short_realm_slug_rejected(dept, capsys, slug):
    dept.edit(lambda c: c["servers"]["groups"]["realms"][0].update(slug=slug))
    err = rerender_error(dept, capsys)
    assert f"realm slug '{slug}' is too short; Zulip needs at least 3 characters" in err


def test_three_char_realm_slug_ok(dept):
    dept.edit(lambda c: c["servers"]["groups"]["realms"][0].update(slug="abc"))
    dept.render()


def test_dedicated_path_alias_may_be_short(dept):
    dept.edit(lambda c: c["servers"]["lab"].update(slug="lb"))
    dept.render()


@pytest.mark.parametrize("path", ["dedicated", "shared"])
def test_realm_name_limited_to_40(dept, capsys, path):
    name = "N" * 41

    def mutate(c):
        if path == "dedicated":
            c["servers"]["dept"]["realm"]["name"] = name
        else:
            c["servers"]["groups"]["realms"][0]["name"] = name
    dept.edit(mutate)
    assert "is 41 characters; Zulip allows at most 40" in rerender_error(dept, capsys)
    dept.edit(lambda c: (c["servers"]["dept"]["realm"].update(name="N" * 40) if path == "dedicated"
                         else c["servers"]["groups"]["realms"][0].update(name="N" * 40)))
    dept.render()


# ---------------------------------------------------------------- 7. consecutive hyphens


def test_prefix_with_double_hyphen_rejected(dept, capsys):
    dept.edit(lambda c: c["azure"].update(prefix="orfe--chat"))
    err = rerender_error(dept, capsys)
    assert "schema validation failed" in err and "azure/prefix" in err


def test_server_name_with_double_hyphen_rejected(dept, capsys):
    dept.edit(add_server("a--b"))
    err = rerender_error(dept, capsys)
    assert "schema validation failed" in err and "'a--b' does not match" in err


# ---------------------------------------------------------------- 8. realm_domain under external_host


@pytest.mark.parametrize("realm_domain", ["r.groups.chat.orfe.example.edu", "a.b.groups.chat.orfe.example.edu"])
def test_realm_domain_under_external_host_rejected(dept, capsys, realm_domain):
    dept.edit(lambda c: c["servers"]["groups"].update(realm_domain=realm_domain))
    err = rerender_error(dept, capsys)
    assert f"servers.groups: realm_domain {realm_domain} is under external_host groups.chat.orfe.example.edu" in err


def test_realm_domain_equal_to_external_host_ok(dept):
    dept.edit(lambda c: c["servers"]["groups"].update(realm_domain="groups.chat.orfe.example.edu"))
    dept.render()


def test_fixture_realm_domain_is_a_parent_of_external_host(dept):
    # tests/fixtures/orfe: external_host groups.chat.orfe.example.edu, realm_domain chat.orfe.example.edu.
    assert dept.server("groups")["hosts"][0] == "groups.chat.orfe.example.edu"


# ---------------------------------------------------------------- 9. vnet_cidr


def test_vnet_cidr_with_host_bits_is_a_config_error(dept, capsys):
    dept.edit(lambda c: c["azure"].update(vnet_cidr="10.60.1.0/16"))
    err = rerender_error(dept, capsys)
    assert "azure.vnet_cidr 10.60.1.0/16 has host bits set; use the network address 10.60.0.0/16" in err
    assert "Traceback" not in err
