"""The template's GitHub workflows: supply-chain pins, credentials, privilege split,
concurrency keys, the destructive-action guard in ops.yml, and the release gates.
Workflow shell is extracted from the YAML and run for real against stand-ins."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import ROOT

WF_DIR = ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WF_DIR.glob("*.yml"))
PINNED = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40} # v\d+(\.\d+){0,2}")
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
BASH = shutil.which("bash") or "/bin/bash"


def wf(name: str) -> dict:
    return yaml.safe_load((WF_DIR / name).read_text())


def uses_in(doc) -> list[str]:
    if isinstance(doc, dict):
        return [v for k, v in doc.items() if k == "uses" and isinstance(v, str)] + \
               [u for k, v in doc.items() if k != "uses" for u in uses_in(v)]
    if isinstance(doc, list):
        return [u for v in doc for u in uses_in(v)]
    return []


def step(workflow: dict, job: str, *, name: str | None = None, id: str | None = None) -> dict:
    for s in workflow["jobs"][job]["steps"]:
        if (name and s.get("name") == name) or (id and s.get("id") == id):
            return s
    raise AssertionError(f"no step {name or id} in {job}")


# ---------------------------------------------------------------- supply chain


def test_there_are_workflows():
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "deploy-server.yml", "ops.yml", "release.yml", "update-check.yml",
                                           "build-image.yml", "keepalive.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_by_commit_with_a_version_comment(path):
    found = []
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = USES_LINE.match(line)
        if not m or m["ref"].startswith("./"):
            continue
        found.append(m["ref"])
        full = m["ref"] + m["rest"].rstrip()
        assert PINNED.fullmatch(full), f"{path.name}: `uses: {full}` is not <action>@<40-hex sha> # vX.Y.Z"
    parsed = [u for u in uses_in(yaml.safe_load(path.read_text())) if not u.startswith("./")]
    assert sorted(parsed) == sorted(found), "a `uses:` the line check did not see (flow style?)"
    assert found, f"{path.name}: no actions at all?"


def test_the_pin_check_rejects_tags_branches_and_bare_shas():
    for bad in ("actions/checkout@v7", "actions/checkout@main", "actions/checkout@" + "a" * 40,
                "actions/checkout@" + "a" * 39 + " # v7.0.1", "actions/checkout@" + "a" * 40 + " # latest"):
        assert not PINNED.fullmatch(bad), bad


def test_same_action_same_pin_everywhere():
    pins: dict[str, set[str]] = {}
    for path in WORKFLOWS:
        for line in path.read_text().splitlines():
            m = USES_LINE.match(line)
            if m and not line.lstrip().startswith("#") and "@" in m["ref"]:
                action, sha = m["ref"].split("@")
                pins.setdefault(action, set()).add(f"{sha}{m['rest'].rstrip()}")
    assert all(len(v) == 1 for v in pins.values()), {k: v for k, v in pins.items() if len(v) > 1}


def test_dependabot_still_tracks_github_actions():
    dep = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert any(u["package-ecosystem"] == "github-actions" and u["directory"] == "/" for u in dep["updates"])


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkouts_do_not_persist_credentials(path):
    for job_name, job in yaml.safe_load(path.read_text())["jobs"].items():
        for s in job.get("steps", []):
            if str(s.get("uses", "")).startswith("actions/checkout@"):
                assert (s.get("with") or {}).get("persist-credentials") is False, f"{path.name}:{job_name}"


# ---------------------------------------------------------------- update-check privilege split


def test_update_check_runs_upstream_code_read_only_and_opens_the_pr_separately():
    u = wf("update-check.yml")
    assert u["permissions"] == {"contents": "read"}
    check = u["jobs"]["check"]
    assert "permissions" not in check  # inherits read-only
    runs = " ".join(s.get("run", "") for s in check["steps"])
    assert "tools/updates.py" in runs and "docker compose" in runs
    assert not any("create-pull-request" in s.get("uses", "") for s in check["steps"])
    pr = u["jobs"]["pr"]
    assert pr["needs"] == "check" and pr["permissions"] == {"contents": "write", "pull-requests": "write"}
    # The write job runs nothing from the checkout: no python, zsh, docker, make or scripts/.
    for s in pr["steps"]:
        run = s.get("run", "")
        assert not re.search(r"(^|[\s;&|(])(bash|sh|zsh|python3?|docker|make|pip|npm|source|\.)\s", run), run
        assert not re.search(r"(^|[\s;&|(])\./", run), run
    assert any("create-pull-request" in s.get("uses", "") for s in pr["steps"])
    assert "UPDATE_PR_TOKEN" not in json.dumps(check)


def git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.edu",
                           "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *a],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def template_repo(tmp_path):
    r = tmp_path / "repo"
    for rel in ("image/Dockerfile", "image/sidecars.json", "tests/Dockerfile", "tools/zulip_reserved.py",
                "README.md", ".github/workflows/ci.yml"):
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(f"{rel}\n")
    git(r.parent, "init", "-q", str(r))
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    return r


def apply_step(repo: Path, edit) -> subprocess.CompletedProcess:
    edit(repo)
    git(repo, "add", "-A")
    patch_text = git(repo, "diff", "--cached", "--binary")
    git(repo, "reset", "-q", "--hard")
    tmp = repo.parent / "runner-temp"
    (tmp / "updates").mkdir(parents=True, exist_ok=True)
    (tmp / "updates" / "updates.patch").write_text(patch_text)
    (tmp / "updates" / "summary.md").write_text("### chore(deps)\n")
    s = step(wf("update-check.yml"), "pr", name="Apply the patch (only the pinned files; no repository code runs here)")
    script = s["run"].replace("${{ runner.temp }}", str(tmp))
    assert "${{" not in script
    return subprocess.run([BASH, "-c", script], cwd=repo, capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "PATCH": str(tmp / "updates" / "updates.patch")})


def test_update_pr_applies_a_pin_bump(template_repo):
    r = apply_step(template_repo, lambda p: (p / "image/sidecars.json").write_text("bumped\n"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(template_repo, "diff", "--cached", "--name-only").split() == ["image/sidecars.json"]


@pytest.mark.parametrize("edit,why", [
    (lambda p: (p / ".github/workflows/ci.yml").write_text("evil\n"), "outside the update allowlist"),
    (lambda p: (p / "README.md").write_text("evil\n"), "outside the update allowlist"),
    (lambda p: (p / "image" / "evil.sh").write_text("x\n"), "outside the update allowlist"),
    (lambda p: os.chmod(p / "tests/Dockerfile", 0o755), "re-modes"),
    (lambda p: (p / "tools/zulip_reserved.py").unlink(), "deletes"),
    (lambda p: (p / "image/sidecars.json").write_bytes(bytes(range(256))), "BINARY"),
])
def test_update_pr_refuses_anything_else(template_repo, edit, why):
    r = apply_step(template_repo, edit)
    assert r.returncode != 0, "accepted a patch outside the allowlist"
    assert why in r.stdout + r.stderr
    assert git(template_repo, "diff", "--cached", "--name-only") == ""  # nothing applied


# ---------------------------------------------------------------- concurrency


def eval_group(expr: str, inputs: dict) -> str:
    """Just enough of the Actions expression language for the groups these workflows use."""
    def one(e: str) -> str:
        e = e.strip()
        m = re.fullmatch(r"inputs\.(\w+)", e)
        if m:
            return inputs[m[1]]
        m = re.fullmatch(r"inputs\.(\w+) != '' && format\('([^']*)', inputs\.(\w+)\) \|\| '([^']*)'", e)
        if m:
            return m[2].replace("{0}", inputs[m[3]]) if inputs[m[1]] != "" else m[4]
        raise AssertionError(f"unsupported expression: {e}")
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: one(m[1]), expr)


def group(name: str, job: str) -> str:
    c = wf(name)["jobs"][job]["concurrency"]
    assert c["cancel-in-progress"] is False
    assert "environment" not in c["group"], f"{name}: keyed on the Environment, not the department"
    return c["group"]


@pytest.mark.parametrize("server", ["lab", "dept", "a-b"])
def test_server_ops_and_deploys_share_one_queue(server):
    for env in ("orfe", "orfe-admin"):
        deploy = eval_group(group("deploy-server.yml", "deploy"), {"config_dir": "orfe", "server": server, "environment": env})
        op = eval_group(group("ops.yml", "op"), {"config_dir": "orfe", "server": server, "environment": "orfe-admin"})
        assert deploy == op == f"chat-orfe-{server}"


def test_department_wide_ops_have_one_department_queue():
    op = eval_group(group("ops.yml", "op"), {"config_dir": "orfe", "server": ""})
    assert op == "chat-orfe"
    assert eval_group(group("ops.yml", "op"), {"config_dir": "math", "server": ""}) == "chat-math"


def test_image_builds_never_share_a_server_queue():
    g = eval_group(group("build-image.yml", "build"), {"config_dir": "orfe"})
    assert g == "chat-orfe.image"
    server_re = re.compile(r"[a-z][a-z0-9-]{0,14}[a-z0-9]")  # deploy-server.yml's server shape
    assert not server_re.fullmatch(g.removeprefix("chat-orfe-"))


# ---------------------------------------------------------------- ops.yml Run step


DESTRUCTIVE = ["teardown", "teardown-purge", "teardown-dept", "realm-deactivate", "realm-set-role"]


@pytest.fixture
def ops_sandbox(tmp_path):
    """A workspace whose .chat-template/scripts/* only record that they ran."""
    scripts = tmp_path / ".chat-template" / "scripts"
    scripts.mkdir(parents=True)
    log = tmp_path / "ran.log"
    for name in ("teardown.zsh", "teardown-server.zsh", "realm.zsh", "keepalive.zsh", "healthchecks.zsh",
                 "ip-gate.zsh", "update-server.zsh"):
        (scripts / name).write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    zsh = bindir / "zsh"
    zsh.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    zsh.chmod(zsh.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "orfe" / "generated" / "servers").mkdir(parents=True)
    (tmp_path / "orfe" / "generated" / "servers" / "lab.json").write_text('{"name": "lab"}')
    return tmp_path, bindir, log


def run_op(sandbox, action, environment, arg="", server="lab"):
    root, bindir, log = sandbox
    s = step(wf("ops.yml"), "op", name="Run")
    assert "${{" not in s["run"], "inputs reach the script through env only"
    env_map = wf("ops.yml")["jobs"]["op"]["env"]
    assert env_map["ENVIRONMENT"] == "${{ inputs.environment }}" and env_map["ACTION"] == "${{ inputs.action }}"
    r = subprocess.run([BASH, "-c", s["run"]], cwd=root, capture_output=True, text=True,
                       env={"PATH": f"{bindir}:{os.environ['PATH']}", "CFG": "orfe", "SRV": server, "ARG": arg,
                            "ACTION": action, "ENVIRONMENT": environment, "GITHUB_SERVER_URL": "x",
                            "GITHUB_REPOSITORY": "x", "GITHUB_RUN_ID": "1"})
    return r, (log.read_text() if log.exists() else "")


ARGS = {"realm-deactivate": "math", "realm-set-role": "math a@b.edu admin", "teardown-dept": "--purge"}


@pytest.mark.parametrize("action", DESTRUCTIVE)
@pytest.mark.parametrize("environment", ["orfe", "orfe-administrator", "admin", "orfe-admin-x", ""])
def test_destructive_actions_refuse_a_non_admin_environment(ops_sandbox, action, environment):
    r, ran = run_op(ops_sandbox, action, environment, ARGS.get(action, ""))
    assert r.returncode == 2, r.stdout + r.stderr
    assert f"::error::{action} needs an -admin environment" in r.stdout
    assert ran == ""  # no script started


@pytest.mark.parametrize("action", DESTRUCTIVE)
def test_destructive_actions_run_in_the_admin_environment(ops_sandbox, action):
    r, ran = run_op(ops_sandbox, action, "orfe-admin", ARGS.get(action, ""))
    assert r.returncode == 0, r.stdout + r.stderr
    assert ".chat-template/scripts/" in ran and "--config orfe" in ran


@pytest.mark.parametrize("action", ["realm-list", "restart", "register-push"])
def test_everyday_actions_need_no_admin_environment(ops_sandbox, action):
    r, ran = run_op(ops_sandbox, action, "orfe")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--server lab" in ran


@pytest.mark.parametrize("arg", ["--purge", "  --healthchecks", "--purge --platform --healthchecks", ""])
def test_teardown_dept_accepts_its_ci_flags(ops_sandbox, arg):
    r, ran = run_op(ops_sandbox, "teardown-dept", "orfe-admin", arg, server="")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "teardown.zsh --config orfe" in ran


@pytest.mark.parametrize("arg", ["--also-server dept", "--entra", "--github", "--purgex", "--purge;id", "--server lab"])
def test_teardown_dept_refuses_other_flags(ops_sandbox, arg):
    r, ran = run_op(ops_sandbox, "teardown-dept", "orfe-admin", arg, server="lab")
    assert r.returncode == 2 and "argument must be teardown.zsh flags" in r.stdout
    assert ran == ""


# ---------------------------------------------------------------- release.yml


@pytest.fixture
def release_repo(tmp_path):
    """origin/main with v1.0.0 and v2.0.0 tagged on it, a v1.0.1 hotfix commit on main, and a side branch."""
    r = tmp_path / "rel"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    shas = {}
    for n in ("one", "two", "three"):
        (r / n).write_text(n)
        git(r, "add", "-A")
        git(r, "commit", "-qm", n)
        shas[n] = git(r, "rev-parse", "HEAD").strip()
    git(r, "tag", "v1.0.0", shas["one"])
    git(r, "tag", "v2.0.0", shas["two"])
    git(r, "update-ref", "refs/remotes/origin/main", shas["three"])
    git(r, "checkout", "-q", "-b", "side", shas["one"])
    (r / "side").write_text("side")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "side")
    shas["side"] = git(r, "rev-parse", "HEAD").strip()
    git(r, "checkout", "-q", "main")
    return r, shas


def tag_check(repo: Path, tag: str, sha: str):
    s = step(wf("release.yml"), "release", id="tagcheck")
    assert "${{" not in s["run"]
    assert s["env"]["TAG"] == "${{ github.ref_name }}"
    out = repo.parent / "out"
    out.write_text("")
    r = subprocess.run([BASH, "-c", s["run"]], cwd=repo, capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"], "TAG": tag, "GITHUB_SHA": sha, "GITHUB_OUTPUT": str(out)})
    return r, out.read_text()


def test_release_checkout_has_full_history():
    co = wf("release.yml")["jobs"]["release"]["steps"][0]
    assert co["uses"].startswith("actions/checkout@") and co["with"]["fetch-depth"] == 0


def test_release_of_the_highest_tag_on_main_is_latest(release_repo):
    repo, shas = release_repo
    git(repo, "tag", "v2.1.0", shas["three"])
    r, out = tag_check(repo, "v2.1.0", shas["three"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert out == "latest=true\n"


def test_release_of_an_older_line_is_not_latest(release_repo):
    repo, shas = release_repo
    git(repo, "tag", "v1.0.1", shas["three"])
    r, out = tag_check(repo, "v1.0.1", shas["three"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert out == "latest=false\n"  # v2.0.0 stays Latest


def test_release_compares_versions_numerically(release_repo):
    repo, shas = release_repo
    git(repo, "tag", "v2.10.0", shas["three"])
    git(repo, "tag", "v2.9.0", shas["two"])
    r, out = tag_check(repo, "v2.9.0", shas["two"])
    assert r.returncode == 0 and out == "latest=false\n"
    r, out = tag_check(repo, "v2.10.0", shas["three"])
    assert r.returncode == 0 and out == "latest=true\n"


def test_release_refuses_a_commit_not_on_main(release_repo):
    repo, shas = release_repo
    git(repo, "tag", "v9.0.0", shas["side"])
    r, out = tag_check(repo, "v9.0.0", shas["side"])
    assert r.returncode == 1 and "is not on main" in r.stdout
    assert out == ""


def test_release_refuses_a_non_semver_tag(release_repo):
    repo, shas = release_repo
    r, _ = tag_check(repo, "v2.1.0-rc1", shas["three"])
    assert r.returncode == 1 and "is not vX.Y.Z" in r.stdout


def test_release_refuses_without_origin_main(release_repo):
    repo, shas = release_repo
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    r, _ = tag_check(repo, "v2.1.0", shas["three"])
    assert r.returncode == 1 and "no origin/main" in r.stdout


@pytest.mark.parametrize("latest", ["true", "false"])
def test_release_passes_the_latest_decision_to_gh(tmp_path, latest):
    s = step(wf("release.yml"), "release", name="Release with template.lock")
    assert s["env"]["LATEST"] == "${{ steps.tagcheck.outputs.latest }}"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path}/gh.args"\n')
    gh.chmod(0o755)
    r = subprocess.run([BASH, "-c", s["run"]], cwd=tmp_path, capture_output=True, text=True,
                       env={"PATH": f"{bindir}:{os.environ['PATH']}", "TAG": "v1.2.3", "LATEST": latest,
                            "GITHUB_SHA": "c" * 40, "GITHUB_REPOSITORY": "pu-shd/chat", "GH_TOKEN": "t"})
    assert r.returncode == 0, r.stdout + r.stderr
    args = (tmp_path / "gh.args").read_text().split("\n")
    assert args[:3] == ["release", "create", "v1.2.3"] and f"--latest={latest}" in args and "--verify-tag" in args
    assert json.loads((tmp_path / "template.lock").read_text()) == {"repo": "pu-shd/chat", "ref": "v1.2.3", "sha": "c" * 40}


def test_release_without_a_latest_decision_fails(tmp_path):
    s = step(wf("release.yml"), "release", name="Release with template.lock")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "gh").chmod(0o755)
    r = subprocess.run([BASH, "-c", s["run"]], cwd=tmp_path, capture_output=True, text=True,
                       env={"PATH": f"{bindir}:{os.environ['PATH']}", "TAG": "v1.2.3", "LATEST": "",
                            "GITHUB_SHA": "c" * 40, "GITHUB_REPOSITORY": "pu-shd/chat"})
    assert r.returncode == 1 and "no latest decision" in r.stdout


def test_release_checks_run_before_the_release_and_only_for_tags():
    steps = wf("release.yml")["jobs"]["release"]["steps"]
    ids = [s.get("id") or s.get("name") for s in steps]
    assert ids.index("tagcheck") < ids.index("Release with template.lock")
    for s in steps:
        if s.get("id") == "tagcheck" or s.get("name") == "Release with template.lock":
            assert s["if"] == "startsWith(github.ref, 'refs/tags/v')"
