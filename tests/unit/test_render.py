"""tools/render.py: schema, validation rules, generated files, and deploy-time resolve."""
from __future__ import annotations

import ast
import base64
import json

import pytest

import render
import zulip_reserved

DOMAIN = "happy-sea-123.canadacentral.azurecontainerapps.io"
IMAGE = "ghcr.io/pu-shd/chat:v1@sha256:" + "a" * 64


def rerender_error(dept, capsys) -> str:
    assert render.main(["render", str(dept.path)]) == 2
    return capsys.readouterr().err


def resolve(dept, server, capsys, *extra) -> dict:
    capsys.readouterr()  # drop output from any earlier render
    code = render.main([
        "resolve", "--server", str(dept.path / "generated" / "servers" / f"{server}.json"),
        "--default-domain", DOMAIN, "--image", IMAGE, *extra,
    ])
    out = capsys.readouterr()
    assert code == 0, out.err
    params = json.loads(out.out)["parameters"]
    params["_env"] = {e["name"]: e["value"] for e in params["zulipEnv"]["value"]}
    params["_stderr"] = out.err
    return params


# ---------------------------------------------------------------- render


def test_render_writes_expected_tree(dept):
    gen = dept.path / "generated"
    assert sorted(p.name for p in (gen / "servers").iterdir()) == ["dept.json", "groups.json", "lab.json"]
    index = json.loads((gen / "index.json").read_text())
    assert index["servers"] == ["dept", "groups", "lab"]
    assert index["redirect_host"] == "dept"
    plat = json.loads((gen / "platform.json").read_text())
    assert plat["resource_group"] == "orfe-chat-rg"
    assert plat["names"]["storage"] == "orfechatdata"
    assert plat["network"] == {"vnet": "10.60.0.0/16", "aca_subnet": "10.60.0.0/23", "pg_subnet": "10.60.2.0/24"}
    assert plat["postgres"]["host"] == "orfe-chat-pg.postgres.database.azure.com"


def test_render_is_deterministic_and_check_passes(dept, capsys):
    before = render.tree_files(dept.path / "generated")
    dept.render()
    assert render.tree_files(dept.path / "generated") == before
    assert render.main(["render", str(dept.path), "--check"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_check_detects_stale_generated(dept, capsys):
    dept.edit(lambda c: c["servers"]["groups"]["realms"].append(
        {"slug": "gamma", "name": "Gamma", "owner": {"email": "g@example.edu", "name": "G"}}))
    assert render.main(["render", str(dept.path), "--check"]) == 1
    err = capsys.readouterr().err
    assert "STALE" in err and "gamma" in err


def test_check_detects_hand_edited_file(dept, capsys):
    f = dept.path / "generated" / "servers" / "dept.json"
    f.write_text(f.read_text().replace('"cpu": 2.0', '"cpu": 4.0'))
    assert render.main(["render", str(dept.path), "--check"]) == 1


def test_defaults_pending_dns_and_two_gib_per_vcpu(dept):
    s = dept.server("dept")
    assert s["dns"] == "pending"
    assert (s["cpu"], s["memory"]) == (2.0, "4Gi")
    assert s["app_name"] == "orfe-chat-dept"
    assert s["jobs"] == {"mgmt": "orfe-chat-dept-mgmt", "dbinit": "orfe-chat-dept-dbinit", "hc": "orfe-chat-dept-hc"}


def test_shared_server_hosts_and_callback(dept):
    s = dept.server("groups")
    assert s["hosts"] == [
        "groups.chat.orfe.example.edu", "auth.groups.chat.orfe.example.edu",
        "ahmadi-group.chat.orfe.example.edu", "beta-lab.chat.orfe.example.edu",
    ]
    assert s["oidc_callback_live"] == "https://auth.groups.chat.orfe.example.edu/complete/oidc/"
    assert s["preview_realm"] == "ahmadi-group"  # first active realm by default
    assert [r["preview"] for r in s["realms"]] == [True, False]


def test_redirects_cover_realms_and_dedicated_group_slugs(dept):
    assert dept.server("dept")["redirects"] == [
        {"slug": "ahmadi-group", "server": "groups", "realm": "ahmadi-group"},
        {"slug": "beta-lab", "server": "groups", "realm": "beta-lab"},
        {"slug": "lab", "server": "lab", "realm": ""},
    ]
    assert dept.server("groups")["redirects"] == []


def test_inactive_realm_has_no_host_or_redirect(dept):
    dept.edit(lambda c: c["servers"]["groups"]["realms"][1].update(active=False))
    dept.render()
    assert "beta-lab.chat.orfe.example.edu" not in dept.server("groups")["hosts"]
    assert "beta-lab" not in [r["slug"] for r in dept.server("dept")["redirects"]]


def test_warnings_for_unpinned_client_id_and_gate(dept, capsys):
    dept.render()
    err = capsys.readouterr().err
    assert "servers.lab: entra.client_id not pinned" in err
    assert "servers.lab: ip_gate blocks the mobile apps" in err


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.update(bogus=1), "Additional properties are not allowed"),
    (lambda c: c.update(admin_email="not-an-email"), "admin_email"),
    (lambda c: c["servers"]["dept"].update(cpu=1.0), "servers/dept"),
    (lambda c: c["servers"]["groups"]["realms"][0].update(slug="Bad_Slug"), "servers/groups"),
    (lambda c: c["servers"]["dept"].update(kind="other"), "servers/dept"),
    (lambda c: c["azure"].update(tenant_id="nope"), "azure/tenant_id"),
    (lambda c: c["servers"]["dept"].pop("realm"), "servers/dept"),
])
def test_schema_rejects(dept, capsys, mutate, message):
    dept.edit(mutate)
    err = rerender_error(dept, capsys)
    assert "schema validation failed" in err and message in err


def test_department_must_match_directory(dept, capsys):
    dept.edit(lambda c: c.update(department="other"))
    assert "must match its directory name" in rerender_error(dept, capsys)


@pytest.mark.parametrize("slug", ["api", "team", "streams", "auth", "www"])
def test_reserved_slugs_rejected(dept, capsys, slug):
    dept.edit(lambda c: c["servers"]["groups"]["realms"][0].update(slug=slug))
    assert "reserved by Zulip" in rerender_error(dept, capsys)


def test_route_segment_slug_rejected():
    candidates = [s for s in zulip_reserved.ZULIP_ROUTE_SEGMENTS
                  if not render.is_reserved(s) and render.re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", s)]
    assert candidates, "expected some Zulip route that is not also a reserved subdomain"


def test_route_segment_collision_is_an_error(dept, capsys):
    slug = sorted(s for s in zulip_reserved.ZULIP_ROUTE_SEGMENTS
                  if not render.is_reserved(s) and render.re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", s))[0]
    dept.edit(lambda c: c["servers"]["groups"]["realms"][0].update(slug=slug))
    assert f"would shadow the Zulip route /{slug}" in rerender_error(dept, capsys)


def test_duplicate_slug_across_servers(dept, capsys):
    dept.edit(lambda c: c["servers"]["lab"].update(slug="ahmadi-group"))
    assert "slug 'ahmadi-group' is used by both" in rerender_error(dept, capsys)


def test_duplicate_hostname(dept, capsys):
    dept.edit(lambda c: c["servers"]["lab"].update(host="ahmadi-group.chat.orfe.example.edu"))
    assert "hostname ahmadi-group.chat.orfe.example.edu is used by both" in rerender_error(dept, capsys)


def test_gate_with_live_dns_needs_key_vault_cert(dept, capsys):
    dept.edit(lambda c: c["servers"]["lab"].update(dns="live"))
    assert "needs cert.key_vault_certificate" in rerender_error(dept, capsys)
    dept.edit(lambda c: c["servers"]["lab"].update(cert={"key_vault_certificate": "lab-cert"}))
    dept.render()


def test_gate_needs_a_static_fallback(dept, capsys):
    (dept.path / "pugwips-snapshot.json").unlink()
    assert "no static fallback" in rerender_error(dept, capsys)
    dept.edit(lambda c: c["ip_gate"].update(fallback_url="https://example.edu/gateways.json"))
    dept.render()


def test_malformed_snapshot_rejected(dept, capsys):
    (dept.path / "pugwips-snapshot.json").write_text('{"prefixes": ["1.2.3.0/24"]}')
    assert "snapshot_date" in rerender_error(dept, capsys)


def test_name_length_limit(dept, capsys):
    dept.edit(lambda c: c["azure"].update(prefix="orfe-chat-longer"))
    dept.edit(lambda c: c["servers"].update({"verylongservernm": c["servers"].pop("lab")}))
    assert "exceeds 32 characters" in rerender_error(dept, capsys)


def test_preview_realm_must_be_active(dept, capsys):
    dept.edit(lambda c: c["servers"]["groups"].update(preview_realm="nope"))
    assert "preview_realm 'nope'" in rerender_error(dept, capsys)


def test_ambiguous_redirect_host(dept, capsys):
    dept.edit(lambda c: c["servers"]["lab"].pop("slug"))
    assert "set redirects.host_server" in rerender_error(dept, capsys)
    dept.edit(lambda c: c.update(redirects={"host_server": "dept"}))
    dept.render()


def test_redirect_host_must_be_dedicated(dept, capsys):
    dept.edit(lambda c: c.update(redirects={"host_server": "groups"}))
    assert "must be a dedicated server" in rerender_error(dept, capsys)


def test_smtp_provider_requires_host(dept, capsys):
    dept.edit(lambda c: c.update(email={"provider": "smtp", "from": "x@example.edu"}))
    assert "provider 'smtp' needs host, port, user" in rerender_error(dept, capsys)


# ---------------------------------------------------------------- resolve


def test_resolve_pending_dedicated_uses_app_fqdn(dept, capsys):
    p = resolve(dept, "dept", capsys)
    env = p["_env"]
    assert env["SETTING_EXTERNAL_HOST"] == f"orfe-chat-dept.{DOMAIN}"
    assert "SETTING_REALM_HOSTS" not in env
    assert p["zulipCpu"]["value"] == "1.25" and p["zulipMemory"]["value"] == "2.5Gi"
    assert p["deployApp"]["value"] is True
    assert not any("{{" in v for v in env.values())


def test_resolve_live_dedicated_uses_host(dept, capsys):
    dept.edit(lambda c: c["servers"]["dept"].update(dns="live"))
    dept.render()
    assert resolve(dept, "dept", capsys)["_env"]["SETTING_EXTERNAL_HOST"] == "chat.orfe.example.edu"


def test_resolve_pending_shared_maps_preview_realm_to_fqdn(dept, capsys):
    env = resolve(dept, "groups", capsys)["_env"]
    fqdn = f"orfe-chat-groups.{DOMAIN}"
    assert env["SETTING_EXTERNAL_HOST"] == fqdn
    assert ast.literal_eval(env["SETTING_REALM_HOSTS"]) == {
        "ahmadi-group": fqdn, "beta-lab": "beta-lab.chat.orfe.example.edu"}
    assert "SETTING_SOCIAL_AUTH_SUBDOMAIN" not in env  # callback on the preview host itself


def test_resolve_live_shared_uses_auth_subdomain(dept, capsys):
    dept.edit(lambda c: c["servers"]["groups"].update(dns="live"))
    dept.render()
    env = resolve(dept, "groups", capsys)["_env"]
    assert env["SETTING_EXTERNAL_HOST"] == "groups.chat.orfe.example.edu"
    assert env["SETTING_SOCIAL_AUTH_SUBDOMAIN"] == "auth"
    assert ast.literal_eval(env["SETTING_REALM_HOSTS"])["ahmadi-group"] == "ahmadi-group.chat.orfe.example.edu"


def test_oidc_setting_is_valid_python_with_get_secret(dept, capsys):
    env = resolve(dept, "dept", capsys)["_env"]
    tree = ast.parse(env["SETTING_SOCIAL_AUTH_OIDC_ENABLED_IDPS"], mode="eval")
    idp = tree.body.values[0]
    keys = [k.value for k in idp.keys]
    secret = idp.values[keys.index("secret")]
    assert isinstance(secret, ast.Call) and secret.func.id == "get_secret"
    assert secret.args[0].value == "social_auth_oidc_secret"
    oidc_url = idp.values[keys.index("oidc_url")].value
    assert oidc_url == "https://login.microsoftonline.com/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/v2.0"
    assert idp.values[keys.index("client_id")].value == "11111111-1111-1111-1111-111111111111"
    assert env["ZULIP_AUTH_BACKENDS"] == "GenericOpenIdConnectBackend"


def redirects_of(params) -> str:
    return base64.b64decode(params["_env"]["CHAT_REDIRECTS_B64"]).decode()


def test_redirects_before_dns_point_only_at_reachable_targets(dept, capsys):
    p = resolve(dept, "dept", capsys)
    conf = redirects_of(p)
    assert f"location ~ ^/ahmadi\\-group/?$ {{ return 302 https://orfe-chat-groups.{DOMAIN}/; }}" in conf
    assert f"location ~ ^/lab/?$ {{ return 302 https://orfe-chat-lab.{DOMAIN}/; }}" in conf
    assert "beta" not in conf.split("\n", 2)[2]
    assert "redirect /beta-lab omitted" in p["_stderr"]


def test_redirects_after_dns_point_at_live_hosts(dept, capsys):
    def live(c):
        for s in c["servers"].values():
            s["dns"] = "live"
        c["servers"]["lab"]["cert"] = {"key_vault_certificate": "lab-cert"}
    dept.edit(live)
    dept.render()
    conf = redirects_of(resolve(dept, "dept", capsys))
    assert "return 302 https://beta-lab.chat.orfe.example.edu/;" in conf
    assert "return 302 https://lab.chat.orfe.example.edu/;" in conf


def test_resolve_refuses_unpinned_image(dept, capsys):
    code = render.main(["resolve", "--server", str(dept.path / "generated/servers/dept.json"),
                        "--default-domain", DOMAIN, "--image", "ghcr.io/pu-shd/chat:latest"])
    assert code == 2 and "pinned by digest" in capsys.readouterr().err


def test_resolve_needs_client_id(dept, capsys):
    code = render.main(["resolve", "--server", str(dept.path / "generated/servers/lab.json"),
                        "--default-domain", DOMAIN, "--image", IMAGE, "--ip-rules", "[{}]"])
    assert code == 2 and "no Entra client id" in capsys.readouterr().err


def test_resolve_refuses_gated_server_without_rules(dept, capsys):
    code = render.main(["resolve", "--server", str(dept.path / "generated/servers/lab.json"),
                        "--default-domain", DOMAIN, "--image", IMAGE,
                        "--oidc-client-id", "33333333-3333-3333-3333-333333333333"])
    assert code == 2 and "refusing to deploy it open" in capsys.readouterr().err


def test_resolve_refuses_rules_for_ungated_server(dept, capsys):
    code = render.main(["resolve", "--server", str(dept.path / "generated/servers/dept.json"),
                        "--default-domain", DOMAIN, "--image", IMAGE, "--ip-rules", '[{"name": "x"}]'])
    assert code == 2 and "ip_gate is off" in capsys.readouterr().err


def test_resolve_passes_live_state_through(dept, capsys):
    domains = [{"name": "chat.orfe.example.edu", "bindingType": "SniEnabled", "certificateId": "/x"}]
    p = resolve(dept, "dept", capsys, "--custom-domains", json.dumps(domains), "--deploy-app", "false")
    assert p["customDomains"]["value"] == domains
    assert p["deployApp"]["value"] is False


def test_urls_lists_now_and_live(dept, capsys):
    assert render.main(["urls", str(dept.path), "--default-domain", DOMAIN, "--json"]) == 0
    rows = {(r["server"], r["realm"]): r for r in json.loads(capsys.readouterr().out)}
    assert rows[("dept", "(root)")]["now"] == f"https://orfe-chat-dept.{DOMAIN}/"
    assert rows[("groups", "beta-lab")]["now"] == "not reachable until DNS is live"
    assert rows[("groups", "beta-lab")]["live"] == "https://beta-lab.chat.orfe.example.edu/"


def test_pylit_round_trips_through_python():
    value = {"a": [True, False, None, 1, 2.5, "q\"uote"], "b": {"c": "d"}}
    assert ast.literal_eval(render.pylit(value)) == value
    with pytest.raises(TypeError):
        render.pylit(object())


def test_warns_when_admin_mail_domain_is_not_the_sending_domain(dept, capsys):
    dept.render()
    assert "admin_email is @example.edu but mail is sent as @orfe.example.edu" in capsys.readouterr().err
    dept.edit(lambda c: c.update(admin_email="admin@orfe.example.edu"))
    dept.render()
    assert "admin_email is @" not in capsys.readouterr().err


# ---------------------------------------------------------------- healthchecks


def enable_hc(dept, **extra):
    dept.edit(lambda c: c.update(healthchecks={"enabled": True, **extra}))
    dept.render()
    return json.loads((dept.path / "generated" / "platform.json").read_text())["healthchecks"]


def test_healthchecks_off_by_default(dept):
    plat = json.loads((dept.path / "generated" / "platform.json").read_text())
    assert plat["healthchecks"]["enabled"] is False and plat["healthchecks"]["checks"] == []
    assert dept.server("lab")["healthchecks"] == {
        "enabled": False, "health": "orfe-chat-lab-health", "web": "orfe-chat-lab-web", "ip_gate": "orfe-chat-lab-ip-gate"}
    assert dept.server("dept")["healthchecks"]["ip_gate"] is None


def test_healthchecks_checks_and_schedules(dept):
    hc = enable_hc(dept, health_interval_minutes=10, tags=["orfe-it"])
    by_slug = {c["slug"]: c for c in hc["checks"]}
    assert sorted(by_slug) == sorted([
        "orfe-chat-dept-health", "orfe-chat-dept-web", "orfe-chat-groups-health", "orfe-chat-groups-web",
        "orfe-chat-lab-health", "orfe-chat-lab-web", "orfe-chat-lab-ip-gate", "orfe-chat-updates"])
    assert (by_slug["orfe-chat-dept-health"]["timeout"], by_slug["orfe-chat-dept-health"]["grace"]) == (600, 1800)
    assert by_slug["orfe-chat-lab-web"]["timeout"] == 86400
    assert by_slug["orfe-chat-updates"]["timeout"] == 7 * 86400
    assert by_slug["orfe-chat-lab-health"]["tags"] == ["chat", "orfe", "orfe-it", "lab"]
    assert hc["ping_base"] == "https://hc-ping.com"


def test_healthchecks_resolve_params(dept, capsys):
    enable_hc(dept)
    p = resolve(dept, "dept", capsys)
    assert p["healthchecksEnabled"]["value"] is True
    assert p["healthchecksSlug"]["value"] == "orfe-chat-dept-health"
    assert p["healthchecksCron"]["value"] == "*/5 * * * *"
    assert p["hcJobName"]["value"] == "orfe-chat-dept-hc"


def test_hourly_health_interval_cron(dept, capsys):
    enable_hc(dept, health_interval_minutes=60)
    assert resolve(dept, "dept", capsys)["healthchecksCron"]["value"] == "0 * * * *"


@pytest.mark.parametrize("bad", [{"enabled": True, "health_interval_minutes": 7},
                                 {"enabled": True, "ping_base": "http://insecure.example"},
                                 {"enabled": True, "surprise": 1}])
def test_healthchecks_schema(dept, capsys, bad):
    dept.edit(lambda c: c.update(healthchecks=bad))
    assert "schema validation failed" in rerender_error(dept, capsys)


def test_ip_gate_can_be_dropped_entirely(dept, capsys):
    (dept.path / "pugwips-snapshot.json").unlink()
    dept.edit(lambda c: c.update(ip_gate={"enabled": False}))
    assert "ip_gate is true but ip_gate.enabled is false" in rerender_error(dept, capsys)
    dept.edit(lambda c: c["servers"]["lab"].pop("ip_gate"))
    dept.render()  # no snapshot, no fallback needed
    plat = json.loads((dept.path / "generated" / "platform.json").read_text())
    assert plat["ip_gate"]["enabled"] is False
    assert not any(dept.server(s)["ip_gate"] for s in ("dept", "groups", "lab"))


# ---------------------------------------------------------------- email: ACS


def use_acs(dept, domain, **email):
    dept.edit(lambda c: c.update(email={"provider": "acs", "acs": {"domain": domain}, **email}))


def test_acs_custom_domain(dept, capsys):
    use_acs(dept, "orfe.example.edu", **{"from": "donotreply@orfe.example.edu"})
    dept.render()
    err = capsys.readouterr().err
    assert "retires ACS Email on 2028-09-30" in err and "30 mails/minute" in err
    plat = json.loads((dept.path / "generated" / "platform.json").read_text())
    assert plat["email"] == {"provider": "acs", "host": "smtp.azurecomm.net", "port": 587, "user": "orfe-chat-smtp",
                             "from": "donotreply@orfe.example.edu", "from_name": "ORFE Chat"}
    assert plat["acs"] == {"email_service": "orfe-chat-email", "communication_service": "orfe-chat-acs",
                           "domain": "orfe.example.edu", "managed": False, "data_location": "United States",
                           "smtp_username_resource": "orfe-chat-smtp", "entra_app": "orfe-chat-acs-smtp",
                           "senders": []}
    env = resolve(dept, "dept", capsys)["_env"]
    assert (env["SETTING_EMAIL_HOST"], env["SETTING_EMAIL_HOST_USER"]) == ("smtp.azurecomm.net", "orfe-chat-smtp")
    assert env["SETTING_NOREPLY_EMAIL_ADDRESS"] == "donotreply@orfe.example.edu"


def test_acs_custom_sender_needs_approved_quota(dept, capsys):
    use_acs(dept, "orfe.example.edu", **{"from": "noreply@orfe.example.edu"})
    assert "until Microsoft approves a quota increase" in rerender_error(dept, capsys)
    dept.edit(lambda c: c["email"]["acs"].update(custom_senders=True))
    dept.render()
    assert json.loads((dept.path / "generated" / "platform.json").read_text())["acs"]["senders"] == ["noreply"]


def test_acs_managed_domain_resolves_sender_at_deploy(dept, capsys):
    use_acs(dept, "azure-managed")
    dept.render()
    assert "10/hour" in capsys.readouterr().err
    assert render.main(["resolve", "--server", str(dept.path / "generated/servers/dept.json"),
                        "--default-domain", DOMAIN, "--image", IMAGE]) == 2
    assert "pass --acs-mail-from" in capsys.readouterr().err
    env = resolve(dept, "dept", capsys, "--acs-mail-from", "DoNotReply@1234abcd.azurecomm.net")["_env"]
    assert env["SETTING_NOREPLY_EMAIL_ADDRESS"] == "DoNotReply@1234abcd.azurecomm.net"
    assert env["SETTING_DEFAULT_FROM_EMAIL"] == "ORFE Chat <DoNotReply@1234abcd.azurecomm.net>"


@pytest.mark.parametrize("email, message", [
    ({"provider": "acs"}, "needs email.acs.domain"),
    ({"provider": "acs", "acs": {"domain": "orfe.example.edu"}, "from": "noreply@other.example.edu"}, "must be an address @orfe.example.edu"),
    ({"provider": "acs", "acs": {"domain": "azure-managed"}, "from": "noreply@orfe.example.edu"}, "drop email.from"),
    ({"provider": "acs", "acs": {"domain": "orfe.example.edu"}, "from": "donotreply@orfe.example.edu", "host": "x.example.edu"}, "set by provider 'acs'"),
    ({"provider": "resend"}, "needs email.from"),
])
def test_acs_validation(dept, capsys, email, message):
    dept.edit(lambda c: c.update(email=email))
    assert message in rerender_error(dept, capsys)
