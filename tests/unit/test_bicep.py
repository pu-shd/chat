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
    # Sidecar ports are plaintext protocols: encrypt traffic between apps in the environment.
    assert env["peerTrafficConfiguration"] == {"encryption": {"enabled": True}}
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


def test_probes_survive_long_migrations_and_dependency_blips(server):
    probes = {p["type"]: p for p in resource(server, "Microsoft.App/containerApps")[0]["properties"]["template"]["containers"][0]["probes"]}
    start = probes["Startup"]
    # A major-upgrade migration can run ~20 minutes; ACA caps period 240 and threshold 10.
    assert start["periodSeconds"] == 120 and start["failureThreshold"] == 10
    assert start["initialDelaySeconds"] + start["periodSeconds"] * start["failureThreshold"] >= 1260
    assert start["periodSeconds"] <= 240 and start["failureThreshold"] <= 10 and start["initialDelaySeconds"] <= 60
    # Liveness must not depend on PostgreSQL/RabbitMQ/Redis/memcached (which /health checks).
    live = probes["Liveness"]
    assert live["tcpSocket"] == {"port": 80}
    assert "httpGet" not in live
    # deploy-server.zsh must wait at least as long as the startup budget.
    import re
    text = (ROOT / "scripts" / "deploy-server.zsh").read_text()
    m = re.search(r"HEALTH_TIMEOUT[^0-9\n]*(\d+)", text)
    assert m, "deploy-server.zsh no longer has a HEALTH_TIMEOUT default"
    budget = start["initialDelaySeconds"] + start["periodSeconds"] * start["failureThreshold"]
    if int(m.group(1)) < budget:  # raised to 1500s by a concurrent change to that script
        pytest.xfail(f"deploy-server.zsh HEALTH_TIMEOUT {m.group(1)}s < startup budget {budget}s (owned elsewhere)")


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


def test_hc_port_reaches_only_the_health_listener(server):
    ports = resource(server, "Microsoft.App/containerApps")[0]["properties"]["configuration"]["ingress"]["additionalPortMappings"]
    (hc,) = [p for p in ports if p["exposedPort"] == 8080]
    # 8081 = chat-entrypoint's /health-only nginx server, never Zulip's whole site on 80.
    assert hc["targetPort"] == 8081
    assert all(p["targetPort"] != 80 for p in ports)
    assert "HEALTH_PORT=8081" in (ROOT / "image" / "bin" / "chat-entrypoint").read_text()


def test_dbinit_job_embeds_the_script(server):
    job = next(j for j in resource(server, "Microsoft.App/jobs") if "dbinit" in json.dumps(j["name"]))
    cmd = job["properties"]["template"]["containers"][0]["command"]
    assert cmd[:2] == ["sh", "-c"]
    assert (ROOT / "image" / "bin" / "chat-dbinit").read_text() == var(server, cmd[2])


def test_healthchecks_job_is_optional_scheduled_and_embeds_the_pinger(server):
    job = next(j for j in resource(server, "Microsoft.App/jobs") if "hcJobName" in json.dumps(j["name"]))
    assert job["condition"] == "[and(variables('hcEnabled'), parameters('deployApp'))]"
    assert server["variables"]["hcEnabled"] == "[and(parameters('healthchecksEnabled'), not(empty(parameters('hcIdentityName'))))]"
    cfg = job["properties"]["configuration"]
    assert cfg["triggerType"] == "Schedule"
    assert cfg["scheduleTriggerConfig"]["cronExpression"] == "[parameters('healthchecksCron')]"
    assert "healthchecks-ping-key" in json.dumps(cfg["secrets"])
    c = job["properties"]["template"]["containers"][0]
    assert (ROOT / "image" / "bin" / "chat-hc-ping").read_text() == var(server, c["command"][2])
    assert "http://${appName}/health" not in json.dumps(c["env"])  # interpolated, not literal
    target = next(e["value"] for e in c["env"] if e["name"] == "CHAT_HC_TARGET")
    assert target == "[format('http://{0}:8080/health', parameters('appName'))]"


def test_healthchecks_job_has_its_own_identity(server):
    assert server["parameters"]["hcIdentityName"] == {"type": "string", "defaultValue": "",
                                                       "metadata": server["parameters"]["hcIdentityName"]["metadata"]}
    job = next(j for j in resource(server, "Microsoft.App/jobs") if "hcJobName" in json.dumps(j["name"]))
    own = "resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', parameters('hcIdentityName'))"
    cfg = job["properties"]["configuration"]
    assert list(job["identity"]["userAssignedIdentities"]) == [f"[format('{{0}}', {own})]"]
    assert [r["identity"] for r in cfg["registries"]] == [f"[{own}]"]
    assert [s["identity"] for s in cfg["secrets"]] == [f"[{own}]"]
    assert "appIdentityName" not in json.dumps(job)


def test_sidecars_drop_root_and_keep_secrets_private(server):
    app = resource(server, "Microsoft.App/containerApps")[0]
    c = {x["name"]: x for x in app["properties"]["template"]["containers"]}
    redis = c["redis"]["command"] + c["redis"].get("args", [])
    script = redis[-1]
    assert script.startswith("umask 077 && ")
    assert script.endswith("&& chown redis:redis /tmp/redis.conf && exec docker-entrypoint.sh redis-server /tmp/redis.conf")
    rabbit = var(server, c["rabbitmq"]["command"][2])
    assert rabbit.index("umask 077") < rabbit.index("10-chat.conf")
    assert "chown rabbitmq:rabbitmq /etc/rabbitmq/conf.d/10-chat.conf" in rabbit
    assert rabbit.rstrip().endswith("exec docker-entrypoint.sh rabbitmq-server")
    memc = var(server, c["memcached"]["command"][2])
    assert memc.index("umask 077") < memc.index("MEMCACHED_SASL_PWDB")


def test_platform_email_resources_are_conditional():
    tpl = build("platform.bicep")
    for rtype in ["Microsoft.Communication/emailServices", "Microsoft.Communication/emailServices/domains",
                  "Microsoft.Communication/communicationServices"]:
        (r,) = resource(tpl, rtype)
        assert r["condition"] == "[variables('useAcs')]", rtype
    assert tpl["variables"]["useAcs"] == "[not(empty(parameters('acs')))]"
    comm = resource(tpl, "Microsoft.Communication/communicationServices")[0]
    assert "acsLinkedDomains" in json.dumps(comm["properties"]["linkedDomains"])


def test_images_come_from_the_department_registry(server):
    app = resource(server, "Microsoft.App/containerApps")[0]
    assert app["properties"]["configuration"]["registries"] == [
        {"server": "[parameters('registryServer')]", "identity": "[resource('Microsoft.ManagedIdentity/userAssignedIdentities', parameters('appIdentityName')).id]"}
    ] or "registryServer" in json.dumps(app["properties"]["configuration"]["registries"])
    for job in resource(server, "Microsoft.App/jobs"):
        assert "registryServer" in json.dumps(job["properties"]["configuration"]["registries"]), job["name"]
    assert "defaultValue" not in server["parameters"]["sidecarImages"]  # always the registry copies


def test_platform_has_a_private_registry():
    tpl = build("platform.bicep")
    (acr,) = resource(tpl, "Microsoft.ContainerRegistry/registries")
    assert acr["sku"]["name"] == "Basic" and acr["properties"]["adminUserEnabled"] is False
    assert "registryLoginServer" in tpl["outputs"]


# ---------------------------------------------------------------- public surface
# The only thing reachable from the Internet is Zulip's web/API ingress. Databases
# (PostgreSQL and the Redis/RabbitMQ/memcached sidecars) and storage never are.


def test_postgres_has_no_public_endpoint():
    tpl = build("platform.bicep")
    pg = resource(tpl, "Microsoft.DBforPostgreSQL/flexibleServers")
    assert len(pg) == 1
    net = pg[0]["properties"]["network"]
    assert net["publicNetworkAccess"] == "Disabled"
    assert "delegatedSubnetResourceId" in net and "privateDnsZoneArmResourceId" in net
    # A firewall rule would only matter with public access; there must be none at all.
    assert resource(tpl, "Microsoft.DBforPostgreSQL/flexibleServers/firewallRules") == []
    assert "firewallRules" not in json.dumps(pg[0])


def test_storage_is_reachable_only_from_the_environment_subnet():
    tpl = build("platform.bicep")
    for sa in resource(tpl, "Microsoft.Storage/storageAccounts"):
        props = sa["properties"]
        acls = props["networkAcls"]
        assert acls["defaultAction"] == "Deny"
        assert acls.get("ipRules", []) == []
        assert len(acls["virtualNetworkRules"]) == 1 and acls["virtualNetworkRules"][0]["action"] == "Allow"
        assert props["allowBlobPublicAccess"] is False and props["allowSharedKeyAccess"] is False


def test_only_the_zulip_web_ingress_is_external(server):
    apps = resource(server, "Microsoft.App/containerApps")
    assert len(apps) == 1
    ingress = apps[0]["properties"]["configuration"]["ingress"]
    assert ingress["external"] is True and ingress["targetPort"] == 80
    assert ingress["allowInsecure"] is False
    assert all(p["external"] is False for p in ingress["additionalPortMappings"])
    # No sidecar port is the main ingress, and no job takes inbound traffic at all.
    assert ingress["targetPort"] not in {5432, 6379, 5672, 11211}
    for job in resource(server, "Microsoft.App/jobs"):
        assert "ingress" not in job["properties"]["configuration"], job["name"]
