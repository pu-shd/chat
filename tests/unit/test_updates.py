"""tools/updates.py against a fake registry / GitHub API, and tools/probe.py."""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import ssl
from pathlib import Path

import pytest

import probe
import updates
from conftest import FIXTURES, ROOT

D_OLD = "sha256:" + "1" * 64
D_NEW = "sha256:" + "2" * 64
D_HUB = "sha256:" + "3" * 64


class FakeWeb:
    """Routes updates.http() by URL substring; unknown URLs fail the test loudly."""

    def __init__(self):
        self.routes: list[tuple[str, int, dict, bytes]] = []
        self.calls: list[str] = []

    def add(self, needle, body=b"", status=200, headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.routes.append((needle, status, headers or {}, body))

    def __call__(self, url, headers=None, method="GET"):
        self.calls.append(f"{method} {url}")
        for needle, status, hdrs, body in self.routes:
            if needle in url:
                return status, hdrs, body
        raise AssertionError(f"unexpected request: {method} {url}")


@pytest.fixture
def web(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr(updates, "http", w)
    return w


@pytest.fixture
def tree(tmp_path):
    """A copy of the parts of the template updates.py rewrites."""
    for rel in ["image/Dockerfile", "image/sidecars.json", "tests/Dockerfile"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    df = tmp_path / "image/Dockerfile"
    df.write_text(updates.DOCKERFILE_RE.sub(f"ARG ZULIP_IMAGE=ghcr.io/zulip/zulip-server:12.3-0@{D_OLD}", df.read_text()))
    return tmp_path


def registry(web, *, zulip_tags, digest, sidecar_tags=None, bicep="v0.43.8"):
    web.add("ghcr.io/token", {"token": "anon"})
    web.add("ghcr.io/v2/zulip/zulip-server/tags/list", {"tags": zulip_tags})
    web.add("ghcr.io/v2/zulip/zulip-server/manifests/", headers={"docker-content-digest": digest})
    for repo, tags in (sidecar_tags or {}).items():
        web.add(f"hub.docker.com/v2/repositories/{repo}/tags", {"results": [{"name": t} for t in tags], "next": None})
    web.add("auth.docker.io/token", {"token": "anon"})
    web.add("registry-1.docker.io/v2/", headers={"docker-content-digest": D_HUB})
    web.add("api.github.com/repos/Azure/bicep/releases/latest", {"tag_name": bicep})


CURRENT_SIDECARS = {
    "library/redis": ["7.4-alpine"], "library/memcached": ["1.6-alpine"], "library/rabbitmq": ["4.2-alpine"],
    "library/postgres": ["17-alpine"], "curlimages/curl": ["8.16.0"],
}


def run_zulip(tree, capsys, apply=True):
    code = updates.main(["zulip", "--root", str(tree), *(["--apply"] if apply else [])])
    return code, capsys.readouterr()


def test_nothing_to_do(web, tree, capsys, tmp_path_factory, monkeypatch):
    registry(web, zulip_tags=["12.3-0", "12.2-1", "latest"], digest=D_OLD, sidecar_tags=CURRENT_SIDECARS)
    out_file = tmp_path_factory.mktemp("gh") / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    before = {p: p.read_text() for p in tree.rglob("*") if p.is_file()}
    code, out = run_zulip(tree, capsys)
    assert code == 0 and "no updates" in out.out
    assert {p: p.read_text() for p in tree.rglob("*") if p.is_file()} == before
    assert out_file.read_text() == "changed=false\ntitle=no updates\n"


def test_new_zulip_version_bumps_pin_and_regenerates_reserved_names(web, tree, capsys, monkeypatch):
    registry(web, zulip_tags=["12.3-0", "12.10-0", "12.4-1", "13.0-0-rc1"], digest=D_NEW, sidecar_tags=CURRENT_SIDECARS)
    ran = []
    monkeypatch.setattr(updates.subprocess, "run", lambda cmd, check: ran.append(cmd))
    code, out = run_zulip(tree, capsys)
    assert code == 0
    assert f"ARG ZULIP_IMAGE=ghcr.io/zulip/zulip-server:12.10-0@{D_NEW}" in (tree / "image/Dockerfile").read_text()
    assert "Zulip 12.3-0 → 12.10-0" in out.out  # numeric, not lexical, ordering; rc tags ignored
    assert ran and ran[0][-1] == "12.10" and ran[0][-2].endswith("sync-reserved.py")
    assert "changelog" in out.out


def test_upstream_rebuild_of_same_tag_updates_digest_only(web, tree, capsys, monkeypatch):
    registry(web, zulip_tags=["12.3-0"], digest=D_NEW, sidecar_tags=CURRENT_SIDECARS)
    monkeypatch.setattr(updates.subprocess, "run", lambda *a, **k: pytest.fail("no reserved-name sync for a rebuild"))
    code, out = run_zulip(tree, capsys)
    assert code == 0 and "rebuilt upstream" in out.out
    assert f"12.3-0@{D_NEW}" in (tree / "image/Dockerfile").read_text()


def test_sidecars_stay_within_their_major(web, tree, capsys):
    tags = {**CURRENT_SIDECARS,
            "library/redis": ["7.4-alpine", "7.10-alpine", "8.2-alpine", "7.4.1-alpine", "7.9"],
            "library/postgres": ["17-alpine", "18-alpine"]}
    registry(web, zulip_tags=["12.3-0"], digest=D_OLD, sidecar_tags=tags)
    code, out = run_zulip(tree, capsys)
    sidecars = (tree / "image/sidecars.json").read_text()
    # Tag and digest move together; untouched pins keep their own digest.
    assert f'"docker.io/library/redis:7.10-alpine@{D_HUB}"' in sidecars
    assert json.loads(sidecars)["postgres"] == json.loads((ROOT / "image/sidecars.json").read_text())["postgres"]
    assert "HEAD https://registry-1.docker.io/v2/library/redis/manifests/7.10-alpine" in web.calls
    assert not any("manifests/17-alpine" in c for c in web.calls)
    assert "redis 8.2-alpine is a new major version — not applied" in out.out
    assert "postgres 18-alpine is a new major version" in out.out


def test_shipped_sidecars_are_all_pinned_by_tag_and_digest():
    data = json.loads((ROOT / "image/sidecars.json").read_text())
    images = {k: v for k, v in data.items() if not k.startswith("_")}
    assert set(images) == {"redis", "memcached", "rabbitmq", "postgres", "curl"}
    text = (ROOT / "image/sidecars.json").read_text()
    assert sorted(m.group("key") for m in updates.SIDECAR_RE.finditer(text)) == sorted(images)
    for k, v in images.items():
        assert re.fullmatch(r"docker\.io/[a-z0-9]+/[a-z0-9-]+:[\w.-]+@sha256:[0-9a-f]{64}", v), (k, v)


def test_unpinned_sidecar_is_an_error_not_skipped(web, tree, capsys):
    side = tree / "image/sidecars.json"
    data = json.loads(side.read_text())
    data["redis"] = data["redis"].split("@")[0]
    side.write_text(json.dumps(data, indent=2) + "\n")
    registry(web, zulip_tags=["12.3-0"], digest=D_OLD, sidecar_tags=CURRENT_SIDECARS)
    code, out = run_zulip(tree, capsys)
    assert code == 1 and "redis not pinned" in out.err


def test_sidecar_digest_lookup_failure_is_an_error(web, tree, capsys):
    web.add("registry-1.docker.io/v2/", status=401)
    registry(web, zulip_tags=["12.3-0"], digest=D_OLD,
             sidecar_tags={**CURRENT_SIDECARS, "library/redis": ["7.4-alpine", "7.5-alpine"]})
    code, out = run_zulip(tree, capsys)
    assert code == 1 and "no digest (HTTP 401)" in out.err
    assert "7.5-alpine" not in (tree / "image/sidecars.json").read_text()


def test_bicep_cli_bump(web, tree, capsys):
    registry(web, zulip_tags=["12.3-0"], digest=D_OLD, sidecar_tags=CURRENT_SIDECARS, bicep="v0.47.16")
    run_zulip(tree, capsys)
    assert "ARG BICEP_VERSION=v0.47.16" in (tree / "tests/Dockerfile").read_text()


def test_dry_run_changes_nothing(web, tree, capsys):
    registry(web, zulip_tags=["12.4-0"], digest=D_NEW, sidecar_tags=CURRENT_SIDECARS)
    before = (tree / "image/Dockerfile").read_text()
    code, out = run_zulip(tree, capsys, apply=False)
    assert code == 0 and "12.3-0 → 12.4-0" in out.out
    assert (tree / "image/Dockerfile").read_text() == before


def test_lookup_failure_is_an_error_not_no_updates(web, tree, capsys):
    web.add("ghcr.io/token", {"token": "anon"})
    web.add("ghcr.io/v2/zulip/zulip-server/tags/list", status=503)
    code, out = run_zulip(tree, capsys)
    assert code == 1 and "update check failed" in out.err and "no updates" not in out.out


# ---------------------------------------------------------------- template (config repo)


@pytest.fixture
def config_repo(tmp_path):
    repo = tmp_path / "chat-config"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "template.lock").write_text(json.dumps({"_note": "keep me", "repo": "pu-shd/chat", "ref": "v0.1.0",
                                                    "image": f"ghcr.io/pu-shd/chat:v0.1.0@{D_OLD}"}))
    (repo / ".github/workflows/deploy.yml").write_text(
        "jobs:\n  d:\n    uses: pu-shd/chat/.github/workflows/deploy-server.yml@v0.1.0\n"
        "  o:\n    uses: pu-shd/chat/.github/workflows/ops.yml@v0.1.0\n    uses-note: other/repo@v0.1.0\n")
    return repo


SHA_NEW = "c" * 40


def release(web, tag, image, asset=True, sha=SHA_NEW, tag_sha=None, on_main="behind", compare_status=200):
    web.add(f"api.github.com/repos/pu-shd/chat/compare/main...{sha}", {"status": on_main}, status=compare_status)
    rel = {"tag_name": tag, "html_url": f"https://github.com/pu-shd/chat/releases/tag/{tag}",
           "assets": [{"name": "template.lock", "browser_download_url": f"https://dl.example/{tag}/template.lock"}] if asset else []}
    web.add("api.github.com/repos/pu-shd/chat/releases/latest", rel)
    web.add(f"https://dl.example/{tag}/template.lock", {"repo": "pu-shd/chat", "ref": tag, "sha": sha})
    web.add(f"api.github.com/repos/pu-shd/chat/commits/{tag}", {"sha": tag_sha or sha})


def test_template_bump_rewrites_lock_and_uses(web, config_repo, capsys):
    release(web, "v0.2.0", f"ghcr.io/pu-shd/chat:v0.2.0@{D_NEW}")
    assert updates.main(["template", "--config-repo", str(config_repo), "--apply"]) == 0
    lock = json.loads((config_repo / "template.lock").read_text())
    assert lock == {"_note": "keep me", "repo": "pu-shd/chat", "ref": "v0.2.0", "sha": SHA_NEW}  # image dropped
    wf = (config_repo / ".github/workflows/deploy.yml").read_text()
    assert wf.count(f"@{SHA_NEW} # v0.2.0") == 2 and "other/repo@v0.1.0" in wf
    assert "pu-shd/chat v0.1.0 → v0.2.0" in capsys.readouterr().out


def test_template_bump_accepts_the_tip_of_main(web, config_repo, capsys):
    release(web, "v0.2.0", "x", on_main="identical")
    assert updates.main(["template", "--config-repo", str(config_repo), "--apply"]) == 0
    assert json.loads((config_repo / "template.lock").read_text())["sha"] == SHA_NEW


@pytest.mark.parametrize("on_main,status", [("diverged", 200), ("ahead", 200), (None, 404)])
def test_template_bump_refuses_a_commit_not_on_main(web, config_repo, capsys, on_main, status):
    release(web, "v0.2.0", "x", on_main=on_main, compare_status=status)
    before = {p: p.read_text() for p in config_repo.rglob("*") if p.is_file()}
    assert updates.main(["template", "--config-repo", str(config_repo), "--apply"]) == 1
    assert "is not on main" in capsys.readouterr().err
    assert {p: p.read_text() for p in config_repo.rglob("*") if p.is_file()} == before
    assert f"GET https://api.github.com/repos/pu-shd/chat/compare/main...{SHA_NEW}" in web.calls


@pytest.mark.parametrize("tag", ["v0.0.9", "v0.1.0"])
def test_template_bump_refuses_a_downgrade_or_same_version(web, config_repo, capsys, tag):
    release(web, tag, "x")
    assert updates.main(["template", "--config-repo", str(config_repo), "--apply"]) == 1
    assert "refusing a downgrade" in capsys.readouterr().err
    assert json.loads((config_repo / "template.lock").read_text())["ref"] == "v0.1.0"


def test_template_bump_compares_versions_numerically(web, config_repo, capsys):
    lock = json.loads((config_repo / "template.lock").read_text())
    (config_repo / "template.lock").write_text(json.dumps({**lock, "ref": "v0.9.0"}))
    release(web, "v0.10.0", "x")
    assert updates.main(["template", "--config-repo", str(config_repo), "--apply"]) == 0
    assert json.loads((config_repo / "template.lock").read_text())["ref"] == "v0.10.0"


def test_template_bump_refuses_a_non_semver_tag(web, config_repo, capsys):
    release(web, "nightly", "x")
    assert updates.main(["template", "--config-repo", str(config_repo), "--apply"]) == 1
    assert "is not vX.Y.Z" in capsys.readouterr().err


def test_template_bump_refuses_a_moved_tag(web, config_repo, capsys):
    release(web, "v0.2.0", f"ghcr.io/pu-shd/chat:v0.2.0@{D_NEW}", tag_sha="d" * 40)
    assert updates.main(["template", "--config-repo", str(config_repo), "--apply"]) == 1
    assert "tag does not point at the released commit" in capsys.readouterr().err
    assert json.loads((config_repo / "template.lock").read_text())["ref"] == "v0.1.0"


def test_template_current(web, config_repo, capsys):
    lock = json.loads((config_repo / "template.lock").read_text())
    (config_repo / "template.lock").write_text(json.dumps({**lock, "sha": SHA_NEW}))
    release(web, "v0.1.0", f"ghcr.io/pu-shd/chat:v0.1.0@{D_OLD}")
    assert updates.main(["template", "--config-repo", str(config_repo), "--apply"]) == 0
    assert "is current" in capsys.readouterr().out


def test_release_without_lock_asset_is_an_error(web, config_repo, capsys):
    release(web, "v0.2.0", "x", asset=False)
    assert updates.main(["template", "--config-repo", str(config_repo)]) == 1
    assert "no template.lock asset" in capsys.readouterr().err


def test_malformed_lock_asset_is_an_error(web, config_repo, capsys):
    release(web, "v0.2.0", "x", sha="not-a-sha")
    assert updates.main(["template", "--config-repo", str(config_repo)]) == 1
    assert "malformed template.lock" in capsys.readouterr().err


# ---------------------------------------------------------------- pugwips snapshot


def snapshot(tmp_path, prefixes, date, mode="vendor"):
    p = tmp_path / "orfe" / "pugwips-snapshot.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps({"_note": "n", "snapshot_date": date, "prefix_mode": mode, "prefixes": prefixes}))
    return p


LIVE = ["192.0.2.97/32", "198.51.0.0/17", "203.0.112.0/20"]


def test_prefixes_follow_pugwips_rules():
    gw = json.loads((FIXTURES / "pugwips" / "gateways.json").read_text())
    assert updates.prefixes_from_gateways(gw, "vendor") == LIVE
    assert updates.prefixes_from_gateways(gw, "slash24") == ["192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"]
    del gw["gateways"]["us-northeast.gw.example"]["vendor_prefixes"]
    assert "198.51.100.0/24" in updates.prefixes_from_gateways(gw, "vendor")  # degrades to cidrs, never to exact


def test_changed_ranges_rewrite_snapshot(tmp_path, capsys):
    snap = snapshot(tmp_path, ["1.2.3.0/24", "198.51.0.0/17"], dt.date.today().isoformat())
    rc = updates.main(["pugwips", "--gateways", str(FIXTURES / "pugwips/gateways.json"), "--snapshot", str(snap),
                       "--mode", "vendor", "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(snap.read_text())
    assert data["prefixes"] == LIVE and data["snapshot_date"] == dt.date.today().isoformat() and data["_note"] == "n"
    assert "+2 −1 prefixes" in out and "removed 1.2.3.0/24" in out


def test_unchanged_recent_snapshot_left_alone(tmp_path, capsys):
    snap = snapshot(tmp_path, LIVE, (dt.date.today() - dt.timedelta(days=3)).isoformat())
    before = snap.read_text()
    assert updates.main(["pugwips", "--gateways", str(FIXTURES / "pugwips/gateways.json"), "--snapshot", str(snap),
                         "--mode", "vendor", "--apply"]) == 0
    assert snap.read_text() == before and "no updates" in capsys.readouterr().out


def test_unchanged_old_snapshot_gets_a_fresh_date(tmp_path, capsys):
    snap = snapshot(tmp_path, LIVE, (dt.date.today() - dt.timedelta(days=20)).isoformat())
    updates.main(["pugwips", "--gateways", str(FIXTURES / "pugwips/gateways.json"), "--snapshot", str(snap),
                  "--mode", "vendor", "--apply"])
    assert json.loads(snap.read_text())["snapshot_date"] == dt.date.today().isoformat()
    assert "snapshot date refreshed" in capsys.readouterr().out


def test_mode_change_rewrites(tmp_path):
    snap = snapshot(tmp_path, LIVE, dt.date.today().isoformat(), mode="slash24")
    updates.main(["pugwips", "--gateways", str(FIXTURES / "pugwips/gateways.json"), "--snapshot", str(snap),
                  "--mode", "vendor", "--apply"])
    assert json.loads(snap.read_text())["prefix_mode"] == "vendor"


def test_empty_gateways_is_an_error(tmp_path, capsys):
    gw = tmp_path / "gw.json"
    gw.write_text('{"gateways": {}}')
    snap = snapshot(tmp_path, LIVE, "2026-01-01")
    assert updates.main(["pugwips", "--gateways", str(gw), "--snapshot", str(snap), "--mode", "vendor"]) == 1
    assert "no gateways" in capsys.readouterr().err


# ---------------------------------------------------------------- probe.py


def test_days_until():
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert probe.days_until(soon) == 10
    assert probe.days_until("2020-01-01T00:00:00+00:00") < 0


def test_cert_days_fixture(monkeypatch, capsys):
    monkeypatch.setenv("CHAT_PROBE_FIXTURE", json.dumps({"chat.example.edu": 12}))
    assert probe.main(["cert-days", "chat.example.edu"]) == 0 and capsys.readouterr().out.strip() == "12"
    assert probe.main(["cert-days", "other.example.edu"]) == 2


def test_cert_days_reads_not_after(monkeypatch):
    class FakeTLS:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def getpeercert(self):
            when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=40, hours=2)
            return {"notAfter": when.strftime("%b %d %H:%M:%S %Y GMT")}

    class FakeCtx:
        def wrap_socket(self, sock, server_hostname):
            assert server_hostname == "chat.example.edu"
            return FakeTLS()

    monkeypatch.delenv("CHAT_PROBE_FIXTURE", raising=False)
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda: FakeCtx())
    monkeypatch.setattr(probe.socket, "create_connection", lambda addr, timeout: FakeTLS())
    assert probe.cert_days("chat.example.edu") == 40


def test_probe_rejects_bad_input(capsys):
    assert probe.main(["days-until", "not-a-date"]) == 2
    assert probe.main(["what"]) == 2


def test_cidrs_validation():
    assert probe.cidrs(["192.0.2.97", "198.51.0.0/17", "198.51.0.0/17"]) == ["192.0.2.97/32", "198.51.0.0/17"]
    for bad in (["0.0.0.0/0"], ["10.0.0.0/7"], ["198.51.0.1/17"], ["2001:db8::/32"], ["nonsense"], []):
        with pytest.raises(ValueError):
            probe.cidrs(bad)
    with pytest.raises(ValueError):
        probe.cidrs([f"10.0.{i}.0/24" for i in range(201)])
