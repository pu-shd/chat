"""Runs inside the e2e compose network (tests/e2e/run.zsh), against a booted Zulip.

Requests carry X-Forwarded-Proto: https, as Container Apps ingress does; the runner is
inside LOADBALANCER_IPS like the ingress proxy is.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit, urlunsplit

import pytest
import requests

if os.environ.get("CHAT_E2E") != "1":
    pytest.skip("e2e runs only via tests/e2e/run.zsh", allow_module_level=True)

BASE = "http://chat.e2e.test"
XFP = {"X-Forwarded-Proto": "https"}


def get(path, **kw):
    kw.setdefault("allow_redirects", False)
    return requests.get(BASE + path, headers={**XFP, **kw.pop("headers", {})}, timeout=30, **kw)


def test_server_settings_reflect_rendered_config():
    r = get("/api/v1/server_settings")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["realm_name"] == "E2E Department"
    assert data["realm_url"] == "https://chat.e2e.test"
    methods = {m["name"]: m for m in data["external_authentication_methods"]}
    assert methods["oidc:entra"]["display_name"] == "Princeton (Microsoft Entra ID)"
    assert data["authentication_methods"]["password"] is False  # OIDC only


@pytest.mark.parametrize("path", ["/ahmadi-group", "/ahmadi-group/"])
def test_group_path_redirects_to_its_subdomain(path):
    r = get(path)
    assert r.status_code == 302
    assert r.headers["Location"] == "https://ahmadi-group.chat.e2e.test/"


def test_non_group_path_is_not_redirected():
    r = get("/ahmadi-groupies")
    assert r.headers.get("Location") != "https://ahmadi-group.chat.e2e.test/"


def test_health_trusts_the_proxy_but_not_forwarded_clients():
    assert get("/health").status_code == 200  # the ingress proxy / probes
    r = get("/health", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403  # a public client, relayed by the proxy


def _plain(url: str) -> str:
    parts = urlsplit(url)
    if parts.hostname == "chat.e2e.test":
        return urlunsplit(("http", parts.netloc, parts.path, parts.query, parts.fragment))
    return url


def test_entra_oidc_sign_in_round_trip():
    s = requests.Session()
    url = BASE + "/accounts/login/social/oidc/entra"
    seen = []
    for _ in range(12):
        r = s.get(url, headers=XFP if "chat.e2e.test" in url else {}, allow_redirects=False, timeout=30)
        for c in s.cookies:  # Zulip marks cookies Secure; the test network is plain HTTP
            c.secure = False
        seen.append((r.status_code, url))
        if r.status_code not in (301, 302, 303):
            break
        url = _plain(requests.compat.urljoin(url, r.headers["Location"]))
    assert any("oidc:8080/entra/authorize" in u for _, u in seen), seen
    me = s.get(BASE + "/json/users/me", headers=XFP, timeout=30)
    assert me.status_code == 200, (seen, me.text[:300])
    # "email" is Zulip's privacy-preserving address; the real one is delivery_email.
    assert me.json()["delivery_email"] == "owner@e2e.test"
    assert me.json()["role"] == 100  # the realm owner created by the mgmt job


def test_outgoing_mail_reached_the_smtp_relay():
    r = requests.get("http://mailpit:8025/api/v1/messages", timeout=30)
    assert r.status_code == 200
    to = [a["Address"] for m in r.json()["messages"] for a in m["To"]]
    assert "e2e-check@e2e.test" in to


def test_hc_job_pinged_success_then_fail():
    pings = requests.get("http://hc:8000/_pings", timeout=30).json()
    paths = [p["path"] for p in pings]
    assert len(paths) == 2, paths  # exactly one ping per probe
    assert paths[0] == "/e2e-ping-key/e2e-chat-dept-health?create=1"
    assert paths[1] == "/e2e-ping-key/e2e-chat-dept-health/fail?create=1"
    # Anything but /health is rejected (Django refuses the internal Host header), so the
    # probe reports it; /health works only because Zulip's nginx pins the Host.
    assert re.search(r"returned [45]\d\d", pings[1]["body"]), pings[1]["body"]


def burst(path, client_ip, n):
    return [get(path, headers={"X-Forwarded-For": client_ip}).status_code for _ in range(n)]


def test_rate_limits_sign_in_per_client_ip():
    codes = burst("/accounts/login/", "198.51.100.7", 8)
    assert 429 in codes, codes          # burst 2 at 6/min: the 4th request is over
    assert codes[0] != 429, codes
    other = burst("/accounts/login/", "198.51.100.8", 1)
    assert other[0] != 429, other       # per IP: another client is unaffected


def test_rate_limits_api_per_client_ip():
    codes = burst("/api/v1/server_settings", "198.51.100.9", 20)
    assert 429 in codes, codes


def test_exempt_range_is_never_limited():
    codes = burst("/accounts/login/", "192.0.2.10", 12)
    assert 429 not in codes, codes


def test_static_and_health_are_not_limited():
    assert 429 not in burst("/health", "198.51.100.7", 10)
