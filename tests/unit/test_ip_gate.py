"""scripts/ip-gate.zsh: live pugwips → static link → keep existing, never narrower."""
from __future__ import annotations

import json
from datetime import date, timedelta

from conftest import FIXTURES, az_basics

GW = FIXTURES / "pugwips"
LIVE_VENDOR = {"198.51.0.0/17", "203.0.112.0/20", "192.0.2.97/32"}
SNAP_VENDOR = {"192.0.2.97/32", "100.64.0.0/16", "100.65.0.0/16", "203.0.112.0/20",
               "100.66.0.0/20", "198.51.0.0/17", "100.67.0.0/16"}
CAMPUS = {"128.112.0.0/16", "140.180.0.0/16", "204.153.48.0/23", "66.180.176.0/24",
          "66.180.177.0/24", "66.180.180.0/22"}


def emit(run, dept, env=None):
    return run("ip-gate.zsh", "--config", str(dept.path), "--server", "lab", "--emit", env=env or {})


def ranges(result, prefix):
    rules = json.loads(result.stdout)
    return {r["ipAddressRange"] for r in rules if r["name"].startswith(prefix)}


def live_download(shims, exit=0):
    shims.on("gh", r"^release download latest --repo PrincetonUniversity/pugwips", exit=exit, files={
        "{dir}/gateways.json": str(GW / "gateways.json"),
        "{dir}/gateways.json.sig": str(GW / "gateways.json.sig"),
        "{dir}/gateways.json.pem": str(GW / "gateways.json.pem"),
    } if exit == 0 else {})


def test_live_source_with_verified_signature(run, dept, shims):
    live_download(shims)
    shims.on("cosign", r"^verify-blob .*--certificate-identity-regexp \^https://github\\\.com/PrincetonUniversity/pugwips/")
    r = emit(run, dept, {"PUGWIPS_READ_TOKEN": "pat-SENTINEL-4242"})
    assert r.returncode == 0, r.stderr
    assert ranges(r, "pu-vpn-") == LIVE_VENDOR
    assert ranges(r, "pu-campus-") == CAMPUS
    assert ranges(r, "aca-internal") == {"10.60.0.0/23"}
    assert "VPN from live" in r.stderr
    assert all(x["action"] == "Allow" for x in json.loads(r.stdout))
    # The token reaches gh only through the environment, never argv.
    assert not any("SENTINEL-4242" in a for c in shims.calls() for a in c["args"])


def test_no_token_uses_committed_snapshot(run, dept, shims):
    r = emit(run, dept)
    assert r.returncode == 0, r.stderr
    assert ranges(r, "pu-vpn-") == SNAP_VENDOR
    assert "PUGWIPS_READ_TOKEN not set" in r.stderr
    assert "VPN from static (committed pugwips-snapshot.json" in r.stderr
    assert shims.calls("gh") == []


def test_failed_download_falls_back_to_static(run, dept, shims):
    live_download(shims, exit=1)
    r = emit(run, dept, {"PUGWIPS_READ_TOKEN": "pat-SENTINEL-4242"})
    assert r.returncode == 0, r.stderr
    assert "could not download gateways.json" in r.stderr
    assert ranges(r, "pu-vpn-") == SNAP_VENDOR


def test_bad_signature_is_not_trusted(run, dept, shims):
    live_download(shims)
    shims.on("cosign", r"^verify-blob", exit=1)
    r = emit(run, dept, {"PUGWIPS_READ_TOKEN": "pat-SENTINEL-4242"})
    assert r.returncode == 0, r.stderr
    assert "signature did not verify; not trusting it" in r.stderr
    assert ranges(r, "pu-vpn-") == SNAP_VENDOR


def test_stale_snapshot_warns_but_is_used(run, dept):
    snap = json.loads((dept.path / "pugwips-snapshot.json").read_text())
    snap["snapshot_date"] = (date.today() - timedelta(days=90)).isoformat()
    (dept.path / "pugwips-snapshot.json").write_text(json.dumps(snap))
    r = emit(run, dept)
    assert r.returncode == 0, r.stderr
    assert "static snapshot is 90 days old" in r.stderr
    assert ranges(r, "pu-vpn-") == SNAP_VENDOR


def test_narrower_snapshot_is_rejected_then_existing_rules_kept(run, dept, shims):
    snap = json.loads((dept.path / "pugwips-snapshot.json").read_text())
    snap["prefix_mode"] = "exact"
    (dept.path / "pugwips-snapshot.json").write_text(json.dumps(snap))
    az_basics(shims)
    shims.on("az", r"^containerapp show .*ipSecurityRestrictions", [
        {"name": "pu-campus-1", "ipAddressRange": "128.112.0.0/16", "action": "Allow"},
        {"name": "pu-vpn-1", "ipAddressRange": "198.51.0.0/17", "action": "Allow"},
        {"name": "pu-vpn-2", "ipAddressRange": "100.67.0.0/16", "action": "Allow"},
    ])
    r = emit(run, dept)
    assert r.returncode == 0, r.stderr
    assert "refusing to narrow" in r.stderr
    assert "keeping the 2 VPN rules already applied" in r.stderr
    assert ranges(r, "pu-vpn-") == {"198.51.0.0/17", "100.67.0.0/16"}


def test_wider_snapshot_is_also_rejected(run, dept, shims):
    dept.edit(lambda c: c["ip_gate"].update(prefix_mode="slash24"))
    dept.render()
    az_basics(shims)
    shims.on("az", r"^containerapp show", "null")
    r = emit(run, dept)
    assert "refusing to widen" in r.stderr
    assert r.returncode == 3


def test_no_source_at_all_fails_loudly(run, dept, shims):
    (dept.path / "pugwips-snapshot.json").unlink()
    dept.edit(lambda c: c["ip_gate"].update(fallback_url="https://static.example.edu/gw.json"))
    dept.render()
    shims.on("curl", r".", exit=22)
    az_basics(shims)
    shims.on("az", r"^containerapp show", "null")
    r = emit(run, dept)
    assert r.returncode == 3
    assert "refusing to apply a campus-only allowlist" in r.stderr
    assert r.stdout.strip() == ""


def test_ungated_server_is_refused(run, dept):
    r = run("ip-gate.zsh", "--config", str(dept.path), "--server", "dept", "--emit")
    assert r.returncode != 0 and "ip_gate: false" in r.stderr


def test_apply_upserts_and_removes_stale_rules(run, dept, shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-lab --query id", "/subs/x/app")
    shims.on("az", r"^containerapp show .*ipSecurityRestrictions", [
        {"name": "pu-vpn-99", "ipAddressRange": "9.9.9.0/24", "action": "Allow"},
        {"name": "ci-runner-temp", "ipAddressRange": "1.2.3.4/32", "action": "Allow"},
    ])
    shims.on("az", r"^containerapp ingress access-restriction (set|remove) ")
    r = run("ip-gate.zsh", "--config", str(dept.path), "--server", "lab", "--apply")
    assert r.returncode == 0, r.stderr
    sets = [c for c in shims.joined("az") if "access-restriction set" in c]
    removes = [c for c in shims.joined("az") if "access-restriction remove" in c]
    assert len(sets) == len(SNAP_VENDOR) + len(CAMPUS) + 1
    assert any("--rule-name pu-vpn-1 " in c for c in sets)
    assert removes == ["containerapp ingress access-restriction remove -g orfe-chat-rg -n orfe-chat-lab --rule-name pu-vpn-99 --output none"]


def test_apply_keeps_live_temp_rules_but_drops_abandoned_ones(run, dept, shims):
    import time
    now = int(time.time())
    az_basics(shims)
    shims.on("az", r"^containerapp show -g orfe-chat-rg -n orfe-chat-lab --query id", "/subs/x/app")
    shims.on("az", r"^containerapp show .*ipSecurityRestrictions", [
        {"name": "ci-temp-111-1", "ipAddressRange": "1.2.3.4/32", "action": "Allow",
         "description": f"Temporary: CI smoke test, created {now - 60}"},           # a smoke test running now
        {"name": "ci-temp-222-1", "ipAddressRange": "1.2.3.5/32", "action": "Allow",
         "description": f"Temporary: CI smoke test, created {now - 7 * 3600}"},     # its run died hours ago
        {"name": "ci-runner-temp", "ipAddressRange": "1.2.3.6/32", "action": "Allow"},  # legacy, unstamped
        {"name": "pu-vpn-99", "ipAddressRange": "9.9.9.0/24", "action": "Allow"},
    ])
    shims.on("az", r"^containerapp ingress access-restriction (set|remove) ")
    r = run("ip-gate.zsh", "--config", str(dept.path), "--server", "lab", "--apply")
    assert r.returncode == 0, r.stderr
    removed = sorted(c.split("--rule-name ")[1].split()[0] for c in shims.joined("az") if "access-restriction remove" in c)
    assert removed == ["ci-temp-222-1", "pu-vpn-99"]


def temp_rules(shims):
    az_basics(shims)
    shims.on("az", r"^containerapp show .*ipSecurityRestrictions", [
        {"name": "ci-temp-4242-1", "ipAddressRange": "1.2.3.4/32", "action": "Allow"},
        {"name": "ci-temp-4243-2", "ipAddressRange": "1.2.3.5/32", "action": "Allow"},
        {"name": "ci-temp-local", "ipAddressRange": "1.2.3.6/32", "action": "Allow"},
    ])
    shims.on("az", r"^containerapp ingress access-restriction (set|remove) ")


def rule_names(shims, verb):
    return [c.split("--rule-name ")[1].split()[0] for c in shims.joined("az") if f"access-restriction {verb}" in c]


def test_temp_rule_is_named_per_run_and_removes_only_its_own(run, dept, shims):
    temp_rules(shims)
    env = {"GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "1"}
    r = run("ip-gate.zsh", "--config", str(dept.path), "--server", "lab", "--add-temp", "1.2.3.4", env=env)
    assert r.returncode == 0, r.stderr
    sets = [c for c in shims.joined("az") if "access-restriction set" in c]
    assert rule_names(shims, "set") == ["ci-temp-4242-1"]
    assert "--ip-address 1.2.3.4/32" in sets[0] and "created " in sets[0]
    r = run("ip-gate.zsh", "--config", str(dept.path), "--server", "lab", "--remove-temp", env=env)
    assert r.returncode == 0, r.stderr
    assert rule_names(shims, "remove") == ["ci-temp-4242-1"]  # never another run's rule


def test_temp_rule_name_is_sanitized_and_bounded(run, dept, shims):
    temp_rules(shims)
    env = {"GITHUB_RUN_ID": "12345678901234567890123456789$(id)", "GITHUB_RUN_ATTEMPT": "3;rm"}
    r = run("ip-gate.zsh", "--config", str(dept.path), "--server", "lab", "--add-temp", "1.2.3.4", env=env)
    assert r.returncode == 0, r.stderr
    (name,) = rule_names(shims, "set")
    assert name == ("ci-temp-" + "12345678901234567890123456789" + "-3")[:32] and len(name) == 32
    import re
    assert re.fullmatch(r"[a-z0-9-]+", name)


def test_temp_rule_outside_ci_has_a_local_name(run, dept, shims):
    temp_rules(shims)
    r = run("ip-gate.zsh", "--config", str(dept.path), "--server", "lab", "--remove-temp")
    assert r.returncode == 0, r.stderr
    assert rule_names(shims, "remove") == ["ci-temp-local"]


def test_remove_temp_when_absent_says_so(run, dept, shims):
    temp_rules(shims)
    r = run("ip-gate.zsh", "--config", str(dept.path), "--server", "lab", "--remove-temp",
            env={"GITHUB_RUN_ID": "999", "GITHUB_RUN_ATTEMPT": "1"})
    assert r.returncode == 0, r.stderr
    assert rule_names(shims, "remove") == [] and "no temporary rule ci-temp-999-1 present" in r.stderr


def test_temp_rule_needs_bare_ipv4(run, dept, shims):
    r = run("ip-gate.zsh", "--config", str(dept.path), "--server", "lab", "--add-temp", "1.2.3.0/24")
    assert r.returncode != 0 and "bare IPv4" in r.stderr
    assert shims.calls("az") == []


def test_unsigned_fallback_url_is_refused(run, dept, shims):
    dept.edit(lambda c: c["ip_gate"].update(fallback_url="https://static.example.edu/gw.json"))
    dept.render()
    shims.on("curl", r"https://static\.example\.edu/gw\.json -o", files={"{o}": str(GW / "gateways.json")})
    shims.on("curl", r"\.(sig|pem) -o", exit=22)
    az_basics(shims)
    shims.on("az", r"^containerapp show", "null")
    r = emit(run, dept)
    assert r.returncode == 3
    assert "refusing an unsigned download" in r.stderr


def test_signed_fallback_url_is_used(run, dept, shims):
    dept.edit(lambda c: c["ip_gate"].update(fallback_url="https://static.example.edu/gw.json"))
    dept.render()
    shims.on("curl", r"gw\.json\.sig -o", files={"{o}": str(GW / "gateways.json.sig")})
    shims.on("curl", r"gw\.json\.pem -o", files={"{o}": str(GW / "gateways.json.pem")})
    shims.on("curl", r"gw\.json -o", files={"{o}": str(GW / "gateways.json")})
    shims.on("cosign", r"^verify-blob")
    r = emit(run, dept)
    assert r.returncode == 0, r.stderr
    assert ranges(r, "pu-vpn-") == LIVE_VENDOR and "signature verified" in r.stderr


def test_absurdly_wide_ranges_are_rejected(run, dept, shims):
    snap = json.loads((dept.path / "pugwips-snapshot.json").read_text())
    snap["prefixes"] = ["0.0.0.0/0"]
    (dept.path / "pugwips-snapshot.json").write_text(json.dumps(snap))
    az_basics(shims)
    shims.on("az", r"^containerapp show", "null")
    r = emit(run, dept)
    assert r.returncode == 3
    assert "wider than /8" in r.stderr


def test_disabled_department_gate_refuses(run, dept, shims):
    dept.edit(lambda c: (c.update(ip_gate={"enabled": False}), c["servers"]["lab"].pop("ip_gate")))
    dept.render()
    r = emit(run, dept)
    assert r.returncode != 0 and "IP gate is disabled for this department" in r.stderr
    assert shims.calls() == []
