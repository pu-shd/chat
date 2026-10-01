"""Shared fixtures: a rendered department, PATH shims for az/gh/cosign/dig/curl, and a
runner for the zsh scripts. Scripts run for real; only the cloud CLIs are stand-ins."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "tools"))

import render  # noqa: E402

SHIMMED = ["az", "gh", "cosign", "dig", "curl", "docker", "docker-compose", "lsof", "security"]

# What az says (stderr, exit 3) for a resource that does not exist. Scripts tell this apart
# from every other failure (az_exists), so a test's "missing" must look like it.
NOT_FOUND = {"exit": 3, "stderr": "ERROR: (ResourceNotFound) The Resource 'x' under resource group 'orfe-chat-rg' was not found."}
ME = "0b0b0b0b-0000-4000-8000-00000000me00"  # the signed-in operator's object id


class Dept:
    def __init__(self, path: Path):
        self.path = path

    @property
    def yml(self) -> Path:
        return self.path / "chat.yml"

    def cfg(self) -> dict:
        return yaml.safe_load(self.yml.read_text())

    def write(self, cfg: dict) -> None:
        self.yml.write_text(yaml.safe_dump(cfg, sort_keys=False))

    def edit(self, fn) -> None:
        cfg = self.cfg()
        fn(cfg)
        self.write(cfg)

    def render(self) -> None:
        assert render.main(["render", str(self.path)]) == 0

    def server(self, name: str) -> dict:
        return json.loads((self.path / "generated" / "servers" / f"{name}.json").read_text())


@pytest.fixture
def dept(tmp_path) -> Dept:
    d = tmp_path / "orfe"
    shutil.copytree(FIXTURES / "orfe", d)
    out = Dept(d)
    out.render()
    return out


class Shims:
    def __init__(self, root: Path):
        self.bin = root / "shimbin"
        self.bin.mkdir()
        for c in SHIMMED:
            (self.bin / c).symlink_to(ROOT / "tests" / "shims" / "shim.py")
        self.spec_path = root / "shim-spec.json"
        self.log_path = root / "shim-log.jsonl"
        self.log_path.write_text("")
        self.rules: dict[str, list] = {c: [] for c in SHIMMED}

    def on(self, cmd: str, match: str, stdout: str = "", exit: int = 0, **extra) -> "Shims":
        if not isinstance(stdout, str):
            stdout = json.dumps(stdout)
        self.rules[cmd].append({"match": match, "stdout": stdout, "exit": exit, **extra})
        return self

    def calls(self, cmd: str | None = None) -> list[dict]:
        rows = [json.loads(l) for l in self.log_path.read_text().splitlines() if l]
        return [r for r in rows if cmd is None or r["cmd"] == cmd]

    def joined(self, cmd: str) -> list[str]:
        return [" ".join(r["args"]) for r in self.calls(cmd)]

    def flush(self) -> None:
        self.spec_path.write_text(json.dumps(self.rules))


@pytest.fixture
def shims(tmp_path) -> Shims:
    return Shims(tmp_path)


def az_basics(sh: Shims) -> Shims:
    """Rules nearly every script needs: a logged-in account in the right tenant."""
    sh.on("az", r"^account show$", {"tenantId": "x"})
    sh.on("az", r"^account set ")
    sh.on("az", r"^account show --query tenantId", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    sh.on("az", r"^account show --query user.type", "user")
    sh.on("az", r"^ad signed-in-user show --query id -o tsv$", ME)
    return sh


def entra_app(sh: Shims, name: str, app_id: str, owners=(ME,)) -> Shims:
    """One app registration called `name`, owned by `owners` (the operator by default)."""
    sh.on("az", rf"^ad app list --display-name {name} --query \[\]\.appId", app_id)
    sh.on("az", rf"^ad app owner list --id {app_id} --query \[\]\.id", "\n".join(owners))
    return sh


@pytest.fixture
def run(shims):
    def _run(script: str, *args: str, env: dict | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
        shims.flush()
        e = {
            "PATH": f"{shims.bin}:{os.environ['PATH']}",
            "HOME": os.environ.get("HOME", "/tmp"),
            "SHIM_SPEC": str(shims.spec_path),
            "SHIM_LOG": str(shims.log_path),
            "CHAT_PY": sys.executable,
            "NO_COLOR": "1",
            "AZ_RETRY_SLEEP": "0",
            "JOB_POLL": "0",
            "JOB_LOG_SLEEP": "0",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        e.update(env or {})
        return subprocess.run(
            ["zsh", str(ROOT / "scripts" / script), *args],
            env=e, input=stdin or "", capture_output=True, text=True, timeout=120,
        )
    return _run
