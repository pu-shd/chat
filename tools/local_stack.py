#!/usr/bin/env python3
"""Turn a department's chat.yml into a stack that runs on this machine (scripts/local.zsh).

    local_stack.py localize <dept-dir> <out-parent> [--server NAME] [--theme NAME]
    local_stack.py override --params P --server-json S --subnet CIDR --state DIR

localize copies chat.yml to <out-parent>/<department>/ with every host name suffixed by .localhost (macOS, Chrome and
Firefox resolve *.localhost to 127.0.0.1 without /etc/hosts) and DNS forced live, renders
it with tools/render.py, and prints what the stack will serve as JSON.

override takes the settings render.py resolve produced and points them at the compose
services instead of Azure: Postgres, the sidecars, a mock Entra and a mail sink. It
writes override.json (compose) and Caddyfile (the TLS edge that stands in for Container
Apps ingress).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render  # noqa: E402

SUFFIX = ".localhost"
IDP_PORT = 9080
# The mock Entra is reached by the same URL from the browser (published port) and from
# Zulip (compose alias), so the discovery document's endpoints work for both.
IDP_HOST = "login.localhost"
IDP_ISSUER = f"http://{IDP_HOST}:{IDP_PORT}/entra"
CLIENT_ID = "00000000-0000-4000-8000-00000000c0de"
MAILPIT_UI = "http://localhost:8025"
# Cloud-only server settings that never apply locally.
CLOUD_ONLY = ("ip_gate", "cert", "easy_auth")
# Any SMTP relay will do: build_override points Zulip at mailpit.
LOCAL_SMTP = {"provider": "smtp", "host": "mail.localhost", "port": 587, "user": "local"}


class LocalError(Exception):
    pass


def localize_config(cfg: dict, server: str | None, theme: str | None) -> tuple[dict, str]:
    cfg = json.loads(json.dumps(cfg))  # deep copy
    servers = cfg.get("servers") or {}
    if not servers:
        raise LocalError("chat.yml declares no servers")
    if server is None:
        server = "dept" if "dept" in servers else next(iter(servers))
    if server not in servers:
        raise LocalError(f"no server {server!r} in chat.yml (have: {', '.join(servers)})")
    defaults = cfg.setdefault("defaults", {})
    defaults["dns"] = "live"
    # Nothing here reaches Azure or third parties.
    cfg["ip_gate"] = {"enabled": False}
    cfg["healthchecks"] = {"enabled": False}
    for key in CLOUD_ONLY:
        defaults.pop(key, None)
    email = cfg.get("email") or {}
    if email.get("provider") == "acs":
        # ACS needs Azure (and an Azure-managed sender only known at deploy time).
        dept = str(cfg.get("department", "local"))
        cfg["email"] = {**LOCAL_SMTP, "from": email.get("from") or f"noreply@{dept}.localhost",
                        **({"from_name": email["from_name"]} if "from_name" in email else {})}
    for s in servers.values():
        s.pop("dns", None)
        for key in CLOUD_ONLY:
            s.pop(key, None)
        for key in ("host", "external_host", "realm_domain"):
            if key in s:
                s[key] = s[key] + SUFFIX
        s.setdefault("entra", {})["client_id"] = CLIENT_ID
    if theme is not None:
        target = servers[server]
        target["theme"] = theme
        for realm in target.get("realms") or []:
            realm.pop("theme", None)
    return cfg, server


def department_pattern() -> str:
    return json.loads(render.SCHEMA_PATH.read_text())["properties"]["department"]["pattern"]


def local_dir(out_parent: Path, department) -> Path:
    """<out_parent>/<department>, refused unless it is a valid department name strictly
    inside out_parent: localize deletes and recreates it."""
    if not isinstance(department, str) or not re.fullmatch(department_pattern(), department):
        raise LocalError(f"chat.yml department {department!r} is not a valid department name "
                         f"(pattern {department_pattern()}); refusing to touch {out_parent}")
    parent = out_parent.resolve()
    out = (parent / department).resolve()
    if out.parent != parent:
        raise LocalError(f"{out} is not inside {parent}; refusing to touch it")
    return out


def cmd_localize(args: argparse.Namespace) -> int:
    cfg = yaml.safe_load((Path(args.dept_dir) / "chat.yml").read_text())
    if not isinstance(cfg, dict):
        raise LocalError("chat.yml must be a mapping")
    # render.py wants the department's directory named after it.
    out = local_dir(Path(args.out_parent), cfg.get("department"))
    local, server = localize_config(cfg, args.server, args.theme)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "chat.yml").write_text(yaml.safe_dump(local, sort_keys=False))
    try:
        rendered = render.render_config(render.load_config(out), out)
        render.write_tree(rendered, out / render.GENERATED)
    except render.ConfigError as e:
        raise LocalError(f"chat.yml does not render: {e}") from e
    spec = json.loads((out / "generated" / "servers" / f"{server}.json").read_text())
    realms = [r for r in spec["realms"] if r["active"]]
    if not realms:
        raise LocalError(f"server {server} has no active realm to serve")
    print(json.dumps({
        "server": server,
        "server_json": str(out / "generated" / "servers" / f"{server}.json"),
        "hosts": spec["hosts"],
        "realms": [{"slug": r["slug"], "name": r["name"], "owner": r["owner"],
                    "url": f"https://{r['live_host']}/"} for r in realms],
    }))
    return 0


def _set(env: dict, key: str, value: str) -> None:
    if key not in env:
        raise LocalError(f"resolved settings have no {key}; render.py and local_stack.py disagree")
    env[key] = value


def build_override(params: dict, spec: dict, subnet: str) -> dict:
    env = {e["name"]: str(e["value"]) for e in params["parameters"]["zulipEnv"]["value"]}
    _set(env, "SETTING_REMOTE_POSTGRES_HOST", "postgres")
    _set(env, "SETTING_REMOTE_POSTGRES_SSLMODE", "disable")
    _set(env, "SETTING_REDIS_HOST", "redis")
    _set(env, "SETTING_RABBITMQ_HOST", "rabbitmq")
    _set(env, "SETTING_MEMCACHED_LOCATION", "memcached:11211")
    _set(env, "SETTING_EMAIL_HOST", "mailpit")
    _set(env, "SETTING_EMAIL_PORT", "1025")
    _set(env, "SETTING_EMAIL_USE_TLS", "False")
    env.pop("SETTING_EMAIL_USE_SSL", None)
    # The edge (Caddy) is on the compose network; trust its X-Forwarded-* like ACA's.
    _set(env, "LOADBALANCER_IPS", subnet)
    idps = env["SETTING_SOCIAL_AUTH_OIDC_ENABLED_IDPS"]
    entra = "https://login.microsoftonline.com/"
    if entra not in idps:
        raise LocalError("resolved OIDC settings do not point at Entra; nothing to swap")
    start = idps.index(entra)
    end = idps.index("/v2.0", start) + len("/v2.0")
    env["SETTING_SOCIAL_AUTH_OIDC_ENABLED_IDPS"] = idps[:start] + IDP_ISSUER + idps[end:]
    # Zulip's outgoing proxy (smokescreen) refuses private addresses; the mock IdP is one.
    env["CONFIG_http_proxy__allow_ranges"] = subnet
    # Every browser request arrives from the compose network, so without this one person
    # clicking around trips a department's auth limit.
    if env.get("CHAT_RATE_LIMITS"):
        env["CHAT_RATE_LIMIT_EXEMPT"] = f"{env.get('CHAT_RATE_LIMIT_EXEMPT', '')} {subnet}".strip()
    if any("$" in v for v in env.values()):
        raise LocalError("a setting contains '$', which compose would interpolate")

    owner = next(r["owner"] for r in spec["realms"] if r["active"])
    idp_config = {
        "interactiveLogin": False,
        "tokenCallbacks": [{
            "issuerId": "entra", "tokenExpiry": 3600,
            "requestMappings": [{"requestParam": "grant_type", "match": "*", "claims": {
                "sub": owner["email"], "email": owner["email"], "name": owner["name"],
                "preferred_username": owner["email"], "aud": [CLIENT_ID]}}]}],
    }
    db = spec["database"]
    return {"services": {
        "zulip": {"environment": env},
        "mgmt": {"environment": {**env, "AUTO_BACKUP_ENABLED": "False"}},
        "dbinit": {"environment": {"DB_NAME": db["name"], "DB_USER": db["user"]}},
        "oidc": {"environment": {"SERVER_PORT": str(IDP_PORT), "JSON_CONFIG": json.dumps(idp_config)}},
    }}


def caddyfile(hosts: list[str]) -> str:
    for h in hosts:
        if not h.endswith(SUFFIX) or not all(c.isalnum() or c in ".-" for c in h):
            raise LocalError(f"refusing to serve {h!r} locally")
    return (
        "# GENERATED by scripts/local.zsh: the TLS edge standing in for Container Apps ingress.\n"
        "{\n\tlocal_certs\n\tskip_install_trust\n}\n\n"
        + ", ".join(hosts)
        + " {\n\treverse_proxy zulip:80\n}\n"
    )


def cmd_override(args: argparse.Namespace) -> int:
    params = json.loads(Path(args.params).read_text())
    spec = json.loads(Path(args.server_json).read_text())
    state = Path(args.state)
    (state / "override.json").write_text(json.dumps(build_override(params, spec, args.subnet), indent=2))
    (state / "Caddyfile").write_text(caddyfile(spec["hosts"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("localize")
    l.add_argument("dept_dir")
    l.add_argument("out_parent")
    l.add_argument("--server")
    l.add_argument("--theme")
    o = sub.add_parser("override")
    o.add_argument("--params", required=True)
    o.add_argument("--server-json", required=True)
    o.add_argument("--subnet", required=True)
    o.add_argument("--state", required=True)
    args = p.parse_args(argv)
    try:
        return {"localize": cmd_localize, "override": cmd_override}[args.cmd](args)
    except LocalError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
