"""infra/*.bicep compile, and the compiled server template keeps its invariants."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from conftest import ROOT


def bicep_cmd():
    if shutil.which("bicep"):
        return ["bicep"]
    if shutil.which("az"):
        return ["az", "bicep"]
    if os.environ.get("CHAT_REQUIRE_BICEP") == "1":
        raise RuntimeError("bicep required but not installed")
    pytest.skip("bicep not installed (the Docker test image has it)")


def file_args(cmd: list[str], verb: str, path) -> list[str]:
    # Standalone bicep takes the file positionally; `az bicep` wants --file.
    return [*cmd, verb, "--file", str(path)] if cmd[0] == "az" else [*cmd, verb, str(path)]


def build(name: str) -> dict:
    r = subprocess.run([*file_args(bicep_cmd(), "build", ROOT / "infra" / name), "--stdout"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def lint(name: str) -> str:
    r = subprocess.run(file_args(bicep_cmd(), "lint", ROOT / "infra" / name), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return "\n".join(l for l in (r.stdout + r.stderr).splitlines() if "new Bicep release" not in l and l.strip())


@pytest.fixture(scope="module")
def server():
    return build("server.bicep")


def var(tpl, value):
    """Resolve a compiled "[variables('x')]" reference (one level) to its value."""
    import re
    m = re.fullmatch(r"\[variables\('([^']+)'\)\]", value) if isinstance(value, str) else None
    return tpl["variables"][m.group(1)] if m else value


def resource(tpl, rtype):
    res = tpl["resources"]
    items = res.values() if isinstance(res, dict) else res
    return [r for r in items if r["type"] == rtype]


@pytest.mark.parametrize("name", ["platform.bicep", "server.bicep"])
def test_lint_clean(name):
    assert lint(name) == ""


def test_platform_compiles_with_expected_outputs():
    tpl = build("platform.bicep")
    assert {"environmentId", "defaultDomain", "customDomainVerificationId", "identityId"} <= set(tpl["outputs"])
    env = resource(tpl, "Microsoft.App/managedEnvironments")[0]["properties"]
    assert env["workloadProfiles"] == [{"name": "Consumption", "workloadProfileType": "Consumption"}]
    pg = resource(tpl, "Microsoft.DBforPostgreSQL/flexibleServers")[0]["properties"]
    assert pg["network"]["publicNetworkAccess"] == "Disabled"


def test_single_replica_zulip_with_three_sidecars(server):
    app = resource(server, "Microsoft.App/containerApps")[0]
    tmpl = app["properties"]["template"]
    assert tmpl["scale"] == {"minReplicas": 1, "maxReplicas": 1}
    assert [c["name"] for c in tmpl["containers"]] == ["zulip", "redis", "memcached", "rabbitmq"]
    assert app["properties"]["configuration"]["activeRevisionsMode"] == "Single"
    probes = {p["type"]: p for p in tmpl["containers"][0]["probes"]}
    assert probes["Startup"]["httpGet"]["path"] == "/health"
    assert probes["Startup"]["failureThreshold"] * probes["Startup"]["periodSeconds"] >= 600


def test_secrets_mounted_where_docker_zulip_reads_them(server):
    app = resource(server, "Microsoft.App/containerApps")[0]
    volumes = var(server, app["properties"]["template"]["volumes"])
    vol = next(v for v in volumes if v["name"] == "zulip-secrets")
    paths = {s["path"] for s in var(server, vol["secrets"])}
    assert paths == {"zulip__secret_key", "zulip__postgres_password", "zulip__redis_password",
                     "zulip__rabbitmq_password", "zulip__memcached_password",
                     "zulip__social_auth_oidc_secret", "zulip__email_password"}
    mounts = {m["volumeName"]: m["mountPath"] for m in var(server, app["properties"]["template"]["containers"][0]["volumeMounts"])}
    assert mounts == {"data": "/data", "zulip-secrets": "/run/secrets"}


def test_mgmt_job_reaches_sidecars_over_the_environment(server):
    job = next(j for j in resource(server, "Microsoft.App/jobs") if "mgmt" in json.dumps(j["name"]))
    env = var(server, job["properties"]["template"]["containers"][0]["env"])
    text = json.dumps(env)
    assert "SETTING_REDIS_HOST" in text and "parameters('appName')" in text
    ports = resource(server, "Microsoft.App/containerApps")[0]["properties"]["configuration"]["ingress"]["additionalPortMappings"]
    assert {p["exposedPort"] for p in ports} == {6379, 5672, 11211, 8080}
    assert all(p["external"] is False for p in ports)


def test_dbinit_job_embeds_the_script(server):
    job = next(j for j in resource(server, "Microsoft.App/jobs") if "dbinit" in json.dumps(j["name"]))
    cmd = job["properties"]["template"]["containers"][0]["command"]
    assert cmd[:2] == ["sh", "-c"]
    assert (ROOT / "image" / "bin" / "chat-dbinit").read_text() == var(server, cmd[2])


def test_healthchecks_job_is_optional_scheduled_and_embeds_the_pinger(server):
    job = next(j for j in resource(server, "Microsoft.App/jobs") if "hcJobName" in json.dumps(j["name"]))
    assert job["condition"] == "[and(parameters('healthchecksEnabled'), parameters('deployApp'))]"
    cfg = job["properties"]["configuration"]
    assert cfg["triggerType"] == "Schedule"
    assert cfg["scheduleTriggerConfig"]["cronExpression"] == "[parameters('healthchecksCron')]"
    assert "healthchecks-ping-key" in json.dumps(cfg["secrets"])
    c = job["properties"]["template"]["containers"][0]
    assert (ROOT / "image" / "bin" / "chat-hc-ping").read_text() == var(server, c["command"][2])
    assert "http://${appName}/health" not in json.dumps(c["env"])  # interpolated, not literal
    assert "/health" in json.dumps(c["env"])
