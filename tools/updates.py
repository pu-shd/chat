#!/usr/bin/env python3
"""updates.py — find newer versions and rewrite the pins, for the update-check workflows.

    updates.py zulip [--apply] [--root DIR]
        pu-shd/chat itself: the Zulip image (image/Dockerfile, tag + digest), the
        sidecar images in infra/server.bicep (newest release in the SAME major only;
        newer majors are reported, never applied), and the bicep CLI in tests/Dockerfile.
        With a new Zulip version it also regenerates tools/zulip_reserved.py.
    updates.py template --config-repo DIR [--apply]
        a config repo: the latest pu-shd/chat release. Rewrites template.lock (from the
        release's template.lock asset: ref, commit sha, image@digest) and pins every
        `uses: pu-shd/chat/...@<sha> # <tag>`. Refuses a release whose tag has moved.
    updates.py pugwips --gateways FILE --snapshot FILE --mode MODE [--apply]
        a config repo: refresh the IP gate's static snapshot from a gateways.json that
        the caller has already signature-checked. Rewritten when the prefixes change,
        or when the snapshot is older than --refresh-days (default 7) so it never ages out.

Prints a Markdown summary on stdout; with $GITHUB_OUTPUT set also writes changed=true|false
and title=<PR title>. Exit 1 on any lookup failure: an update check that could not check
must not look like "no updates".
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "pu-shd-chat-updates"}


class UpdateError(Exception):
    pass


# ------------------------------------------------------------------ http (patched in tests)


def http(url: str, headers: dict | None = None, method: str = "GET") -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
    except (urllib.error.URLError, TimeoutError) as e:
        raise UpdateError(f"{url}: {e}") from e


def get_json(url: str, headers: dict | None = None):
    status, _, body = http(url, headers)
    if status != 200:
        raise UpdateError(f"{url}: HTTP {status}")
    return json.loads(body)


def github_headers() -> dict:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    return {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {tok}"} if tok else {})}


# ------------------------------------------------------------------ registries

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def ghcr_token(repo: str) -> str:
    return get_json(f"https://ghcr.io/token?scope=repository:{repo}:pull")["token"]


def ghcr_tags(repo: str) -> list[str]:
    tok = ghcr_token(repo)
    tags, url = [], f"https://ghcr.io/v2/{repo}/tags/list?n=1000"
    while url:
        status, headers, body = http(url, {"Authorization": f"Bearer {tok}"})
        if status != 200:
            raise UpdateError(f"{url}: HTTP {status}")
        tags += json.loads(body).get("tags") or []
        link = headers.get("link", "")
        m = re.search(r"<([^>]+)>;\s*rel=\"next\"", link)
        url = ("https://ghcr.io" + m.group(1)) if m else None
    return tags


def ghcr_digest(repo: str, tag: str) -> str:
    tok = ghcr_token(repo)
    status, headers, _ = http(f"https://ghcr.io/v2/{repo}/manifests/{tag}",
                              {"Authorization": f"Bearer {tok}", "Accept": MANIFEST_ACCEPT}, method="HEAD")
    digest = headers.get("docker-content-digest", "")
    if status != 200 or not digest.startswith("sha256:"):
        raise UpdateError(f"ghcr.io/{repo}:{tag}: no digest (HTTP {status})")
    return digest


def dockerhub_tags(repo: str, contains: str) -> list[str]:
    namespace = repo if "/" in repo else f"library/{repo}"
    url = f"https://hub.docker.com/v2/repositories/{namespace}/tags?page_size=100&ordering=last_updated"
    if contains:
        url += f"&name={contains}"
    tags = []
    for _ in range(5):  # newest first; five pages is plenty for one suffix
        data = get_json(url)
        tags += [t["name"] for t in data.get("results", [])]
        url = data.get("next")
        if not url:
            break
    return tags


def github_latest_release(repo: str) -> dict:
    return get_json(f"https://api.github.com/repos/{repo}/releases/latest", github_headers())


# ------------------------------------------------------------------ versions


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


@dataclass
class Report:
    title_parts: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    edits: dict[Path, str] = field(default_factory=dict)
    zulip_version: str | None = None  # set when the Zulip minor version changes

    @property
    def changed(self) -> bool:
        return bool(self.edits)

    def edit(self, path: Path, old: str, new: str) -> None:
        text = self.edits.get(path, path.read_text())
        if old not in text:
            raise UpdateError(f"{path}: expected to find {old!r}")
        self.edits[path] = text.replace(old, new)


# ------------------------------------------------------------------ zulip (template repo)

ZULIP_REPO = "zulip/zulip-server"
ZULIP_TAG = re.compile(r"^(\d+)\.(\d+)-(\d+)$")
DOCKERFILE_RE = re.compile(r"^ARG ZULIP_IMAGE=ghcr\.io/zulip/zulip-server:(?P<tag>[\w.-]+)@(?P<digest>sha256:[0-9a-f]{64})$", re.M)
SIDECAR_RE = re.compile(r"^\s+(?P<key>\w+): '(?P<image>docker\.io/(?P<repo>[\w./-]+):(?P<tag>[\w.-]+))'$", re.M)
BICEP_RE = re.compile(r"^ARG BICEP_VERSION=(v[\d.]+)$", re.M)


def check_zulip(root: Path, rep: Report) -> None:
    dockerfile = root / "image" / "Dockerfile"
    m = DOCKERFILE_RE.search(dockerfile.read_text())
    if not m:
        raise UpdateError(f"{dockerfile}: no pinned ARG ZULIP_IMAGE=ghcr.io/zulip/zulip-server:<tag>@sha256:<digest>")
    cur_tag, cur_digest = m.group("tag"), m.group("digest")
    releases = [t for t in ghcr_tags(ZULIP_REPO) if ZULIP_TAG.match(t)]
    if not releases:
        raise UpdateError(f"ghcr.io/{ZULIP_REPO}: no release tags found")
    latest = max(releases, key=vkey)
    target = latest if vkey(latest) > vkey(cur_tag) else cur_tag
    digest = ghcr_digest(ZULIP_REPO, target)
    if target != cur_tag or digest != cur_digest:
        rep.edit(dockerfile, f"{cur_tag}@{cur_digest}", f"{target}@{digest}")
        what = f"Zulip {cur_tag} → {target}" if target != cur_tag else f"Zulip {cur_tag} rebuilt upstream"
        rep.title_parts.append(what)
        rep.lines.append(f"- **{what}** (`ghcr.io/{ZULIP_REPO}:{target}@{digest[:19]}…`)")
        new_version = ".".join(target.split("-")[0].split(".")[:2])
        old_version = ".".join(cur_tag.split("-")[0].split(".")[:2])
        if new_version != old_version:
            rep.notes.append(f"Zulip {new_version} release notes: https://zulip.readthedocs.io/en/latest/overview/changelog.html "
                             "— check the upgrade notes before merging; the deploy migrates the database")
            rep.notes.append(f"regenerate reserved names: tools/sync-reserved.py {new_version}")
            rep.zulip_version = new_version
    else:
        rep.lines.append(f"- Zulip `{cur_tag}` is current")

    bicep = root / "infra" / "server.bicep"
    for sm in SIDECAR_RE.finditer(bicep.read_text()):
        repo, tag = sm.group("repo"), sm.group("tag")
        repo_name = repo.removeprefix("library/")
        num = re.match(r"^(\d+(?:\.\d+)*)(.*)$", tag)
        if not num:
            continue
        version, suffix = num.group(1), num.group(2)
        pattern = re.compile(rf"^(\d+(?:\.\d+){{{version.count('.')}}}){re.escape(suffix)}$")
        candidates = [t for t in dockerhub_tags(repo, suffix) if pattern.match(t)]
        if not candidates:
            raise UpdateError(f"docker.io/{repo}: no tags like {tag}")
        same_major = [t for t in candidates if vkey(t)[0] == vkey(version)[0]]
        best = max(same_major, key=vkey) if same_major else tag
        if vkey(best) > vkey(tag):
            rep.edit(bicep, sm.group("image"), sm.group("image").replace(f":{tag}", f":{best}"))
            rep.title_parts.append(f"{repo_name} {tag} → {best}")
            rep.lines.append(f"- sidecar **{repo_name}** `{tag}` → `{best}`")
        newest = max(candidates, key=vkey)
        if vkey(newest)[0] > vkey(version)[0]:
            rep.notes.append(f"{repo_name} {newest} is a new major version — not applied; upgrade deliberately")

    tests_df = root / "tests" / "Dockerfile"
    bm = BICEP_RE.search(tests_df.read_text())
    if bm:
        latest_bicep = github_latest_release("Azure/bicep")["tag_name"]
        if vkey(latest_bicep) > vkey(bm.group(1)):
            rep.edit(tests_df, f"ARG BICEP_VERSION={bm.group(1)}", f"ARG BICEP_VERSION={latest_bicep}")
            rep.title_parts.append(f"bicep {latest_bicep}")
            rep.lines.append(f"- bicep CLI `{bm.group(1)}` → `{latest_bicep}` (tests image)")


# ------------------------------------------------------------------ template (config repo)

USES_RE = re.compile(r"(uses:\s*pu-shd/chat/\.github/workflows/[\w.-]+@)(\S+)([ \t]*#[^\n]*)?")


def check_template(config: Path, rep: Report, template_repo: str = "pu-shd/chat") -> None:
    lock_path = config / "template.lock"
    lock = json.loads(lock_path.read_text())
    rel = github_latest_release(template_repo)
    tag = rel["tag_name"]
    asset = next((a for a in rel.get("assets", []) if a["name"] == "template.lock"), None)
    if asset is None:
        raise UpdateError(f"{template_repo} {tag}: release has no template.lock asset")
    status, _, body = http(asset["browser_download_url"], {"Accept": "application/octet-stream"})
    if status != 200:
        raise UpdateError(f"{template_repo} {tag}: template.lock asset HTTP {status}")
    new = json.loads(body)
    if (new.get("ref") != tag or "@sha256:" not in (new.get("image") or "")
            or not re.fullmatch(r"[0-9a-f]{40}", new.get("sha") or "")):
        raise UpdateError(f"{template_repo} {tag}: malformed template.lock asset {new}")
    # The tag must still point at the commit the release was built from.
    status, _, body = http(f"https://api.github.com/repos/{template_repo}/commits/{tag}", github_headers())
    if status != 200 or json.loads(body).get("sha") != new["sha"]:
        raise UpdateError(f"{template_repo} {tag}: tag does not point at the released commit {new['sha']}")
    if lock.get("ref") == tag and lock.get("sha") == new["sha"] and lock.get("image") == new["image"]:
        rep.lines.append(f"- pu-shd/chat `{tag}` is current")
        return
    merged = {**lock, **{k: new[k] for k in ("repo", "ref", "sha", "image")}}
    rep.edits[lock_path] = json.dumps(merged, indent=2) + "\n"
    for wf in sorted((config / ".github" / "workflows").glob("*.yml")):
        text = rep.edits.get(wf, wf.read_text())
        # Pin by commit (a tag can be moved); the tag stays as a comment for humans.
        updated = USES_RE.sub(lambda m: f"{m.group(1)}{new['sha']} # {tag}", text)
        if updated != text:
            rep.edits[wf] = updated
    rep.title_parts.append(f"pu-shd/chat {lock.get('ref')} → {tag}")
    rep.lines.append(f"- **pu-shd/chat {lock.get('ref')} → {tag}**: {rel.get('html_url', '')}")
    rep.notes.append("generated/ is re-rendered with the new template in this PR; merging redeploys every server "
                     "(an image change stops each server briefly while Zulip migrates)")


# ------------------------------------------------------------------ pugwips (config repo)

RANK = {"exact": 1, "slash24": 2, "vendor": 3}


def prefixes_from_gateways(gw: dict, mode: str) -> list[str]:
    """Same rules as pugwips examples/read-prefixes.sh and scripts/ip-gate.zsh."""
    out = []
    for g in gw["gateways"].values():
        ips = [ip for ip in g.get("ips", []) if re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", ip)]
        slash24 = [re.sub(r"\.\d+$", ".0/24", ip) for ip in ips]
        if mode == "exact":
            out += g.get("ips", [])
        elif mode == "slash24":
            out += g["cidrs"] if "cidrs" in g else slash24
        else:
            # jq's has(): a present-but-empty vendor_prefixes contributes nothing, as in ip-gate.zsh
            out += g["vendor_prefixes"] if "vendor_prefixes" in g else (g["cidrs"] if "cidrs" in g else slash24)
    return sorted(set(out), key=lambda p: tuple(int(x) for x in re.findall(r"\d+", p)))


def check_pugwips(gateways: Path, snapshot: Path, mode: str, refresh_days: int, rep: Report,
                  today: dt.date | None = None) -> None:
    today = today or dt.date.today()
    gw = json.loads(gateways.read_text())
    if not gw.get("gateways"):
        raise UpdateError(f"{gateways}: no gateways")
    new_prefixes = prefixes_from_gateways(gw, mode)
    if not new_prefixes:
        raise UpdateError(f"{gateways}: no prefixes in mode {mode}")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import probe  # same validation ip-gate.zsh applies before using any range
    try:
        new_prefixes = probe.cidrs(new_prefixes)
    except ValueError as exc:
        raise UpdateError(f"{gateways}: {exc}") from exc
    old = json.loads(snapshot.read_text()) if snapshot.exists() else {}
    old_prefixes = old.get("prefixes") if old.get("prefix_mode") == mode else None
    age = (today - dt.date.fromisoformat(old["snapshot_date"][:10])).days if old.get("snapshot_date") else None
    if old_prefixes == new_prefixes and age is not None and age < refresh_days:
        rep.lines.append(f"- pugwips snapshot `{snapshot.parent.name}` current ({age} days old, unchanged)")
        return
    resolved = gw.get("resolved_at") or gw.get("generated_at") or today.isoformat()
    doc = {
        "_note": old.get("_note", "Static fallback for the pugwips IP gate; refreshed by the update-check workflow."),
        "snapshot_date": today.isoformat(),
        "source": f"PrincetonUniversity/pugwips release latest (resolved {resolved})",
        "prefix_mode": mode,
        "prefixes": new_prefixes,
    }
    rep.edits[snapshot] = json.dumps(doc, indent=2) + "\n"
    if old_prefixes != new_prefixes:
        added = sorted(set(new_prefixes) - set(old_prefixes or []))
        removed = sorted(set(old_prefixes or []) - set(new_prefixes))
        rep.title_parts.append(f"pugwips {snapshot.parent.name} ranges")
        rep.lines.append(f"- **pugwips `{snapshot.parent.name}`**: +{len(added)} −{len(removed)} prefixes "
                         f"({', '.join(added[:5])}{'…' if len(added) > 5 else ''})")
        if removed:
            rep.notes.append(f"{snapshot.parent.name}: removed {', '.join(removed)} — VPN users on those ranges lose "
                             "access through the static fallback")
    else:
        rep.title_parts.append(f"pugwips {snapshot.parent.name} snapshot date")
        rep.lines.append(f"- pugwips `{snapshot.parent.name}`: ranges unchanged, snapshot date refreshed ({age} days old)")


# ------------------------------------------------------------------ main


def finish(rep: Report, apply: bool, root: Path) -> int:
    for path, text in rep.edits.items():
        if apply:
            path.write_text(text)
    if apply and rep.zulip_version:
        subprocess.run([sys.executable, str(root / "tools" / "sync-reserved.py"), rep.zulip_version], check=True)
    title = "chore(deps): " + ", ".join(rep.title_parts) if rep.title_parts else "no updates"
    print(f"### {title}\n")
    print("\n".join(rep.lines))
    if rep.notes:
        print("\n**Notes**\n")
        print("\n".join(f"- {n}" for n in rep.notes))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"changed={'true' if rep.changed else 'false'}\ntitle={title}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    z = sub.add_parser("zulip")
    z.add_argument("--root", default=str(ROOT))
    t = sub.add_parser("template")
    t.add_argument("--config-repo", required=True)
    t.add_argument("--template-repo", default="pu-shd/chat")
    p = sub.add_parser("pugwips")
    p.add_argument("--gateways", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--mode", required=True, choices=sorted(RANK))
    p.add_argument("--refresh-days", type=int, default=7)
    for s in (z, t, p):
        s.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    rep = Report()
    try:
        if args.cmd == "zulip":
            root = Path(args.root)
            check_zulip(root, rep)
        elif args.cmd == "template":
            root = Path(args.config_repo)
            check_template(root, rep, args.template_repo)
        else:
            root = ROOT
            check_pugwips(Path(args.gateways), Path(args.snapshot), args.mode, args.refresh_days, rep)
    except (UpdateError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: update check failed: {exc}", file=sys.stderr)
        return 1
    return finish(rep, args.apply, root)


if __name__ == "__main__":
    sys.exit(main())
