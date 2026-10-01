"""Image scripts after the 12.3 audit: chat-entrypoint (fatal rate limits, theme hosts,
the health-only listener), chat-manage (deactivation reason, failures are ok:false,
set-role guards) and chat-dbinit (identifier validation, no SQL interpolation,
REVOKE CONNECT FROM PUBLIC). Every script runs for real; only Zulip, su and psql are
stand-ins."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import ROOT

BIN = ROOT / "image" / "bin"


def results(out: str) -> list[dict]:
    """Every CHAT-RESULT line, parsed (a line that is not valid JSON fails the test)."""
    return [json.loads(line.split("CHAT-RESULT: ", 1)[1]) for line in out.splitlines() if "CHAT-RESULT: " in line]


def executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ---------------------------------------------------------------- chat-entrypoint


def run_entrypoint(tmp_path, **env):
    paths = {
        "CHAT_REDIRECTS_CONF": tmp_path / "app.d" / "chat-redirects.conf",
        "CHAT_RATE_HTTP_CONF": tmp_path / "conf.d" / "chat-rate-limits.conf",
        "CHAT_RATE_SERVER_CONF": tmp_path / "app.d" / "chat-rate-limits.conf",
        "CHAT_THEME_HTTP_CONF": tmp_path / "conf.d" / "chat-themes.conf",
        "CHAT_THEME_SERVER_CONF": tmp_path / "app.d" / "chat-themes.conf",
        "CHAT_HEALTH_CONF": tmp_path / "conf.d" / "chat-health.conf",
    }
    full = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    full.update({k: str(v) for k, v in paths.items()})
    full.update({"CHAT_ENTRYPOINT_TEST": "1", "CHAT_THEMES_DIR": str(ROOT / "image" / "themes"), **env})
    r = subprocess.run(["bash", str(BIN / "chat-entrypoint")], env=full, capture_output=True, text=True)
    return r, paths


GOOD_LIMITS = "auth:20r/m:30,api:50r/s:500"


def test_good_rate_limits_boot(tmp_path):
    r, p = run_entrypoint(tmp_path, CHAT_RATE_LIMITS=GOOD_LIMITS, CHAT_RATE_LIMIT_EXEMPT="10.0.0.0/8 192.0.2.0/24")
    assert r.returncode == 0, r.stderr
    assert "zone=chat_auth:10m rate=20r/m" in p["CHAT_RATE_HTTP_CONF"].read_text()
    assert "2 exempt range(s)" in r.stdout


@pytest.mark.parametrize("limits,exempt", [
    ("auth:20r/m:30", ""),                              # api part missing
    ("auth:20r/h:30,api:50r/s:500", ""),               # bad unit
    ("auth:20r/m:30,api:50r/s:500;evil", ""),          # trailing nginx text
    (GOOD_LIMITS, "10.0.0.0/8 not-a-cidr"),
    (GOOD_LIMITS, "10.0.0.0/33"),
    (GOOD_LIMITS, "2001:db8::/32"),
])
def test_malformed_rate_limits_are_fatal(tmp_path, limits, exempt):
    # A typo must stop the boot, never start a server without its abuse protection.
    r, p = run_entrypoint(tmp_path, CHAT_RATE_LIMITS=limits, CHAT_RATE_LIMIT_EXEMPT=exempt)
    assert r.returncode != 0
    assert "FATAL" in r.stderr and "not starting Zulip" in r.stderr
    assert not p["CHAT_RATE_HTTP_CONF"].exists() and not p["CHAT_RATE_SERVER_CONF"].exists()


def test_fatal_rate_limits_stop_before_zulip_starts():
    # Order in the real (non-test) path: our fragments, then the upstream entrypoint.
    text = (BIN / "chat-entrypoint").read_text()
    main = text[text.index('case "${1:-app:run}" in'):]
    steps = [main.index(s) for s in ("write_redirects", "write_rate_limits", "write_themes",
                                     "write_health_listener", "exec /sbin/entrypoint.sh")]
    assert steps == sorted(steps)
    assert "set -euo pipefail" in text


def test_redirect_and_theme_errors_still_soft_fail(tmp_path):
    r, p = run_entrypoint(tmp_path, CHAT_RATE_LIMITS=GOOD_LIMITS, CHAT_REDIRECTS_B64="!!!",
                          CHAT_THEMES="chat.orfe.example.edu=neon")
    assert r.returncode == 0, r.stderr
    assert "not valid base64" in r.stderr and "themes OFF" in r.stderr
    assert p["CHAT_RATE_HTTP_CONF"].exists()


@pytest.mark.parametrize("host", ["include", "default", "hostnames", "volatile", "localhost",
                                  ".example.edu", "example.edu.", "a..b", "-a.b", "a-.b"])
def test_theme_hosts_need_a_dot_and_cannot_be_map_keywords(tmp_path, host):
    r, p = run_entrypoint(tmp_path, CHAT_THEMES=f"{host}=paper-tiger")
    assert r.returncode == 0 and "themes OFF" in r.stderr, r.stderr
    assert not p["CHAT_THEME_HTTP_CONF"].exists()


@pytest.mark.parametrize("host", ["chat.orfe.example.edu", "orfe-chat-dept.x1.io", "a.b"])
def test_dotted_theme_hosts_are_accepted(tmp_path, host):
    r, p = run_entrypoint(tmp_path, CHAT_THEMES=f"{host}=paper-tiger")
    assert r.returncode == 0 and "theme(s) for 1 host(s)" in r.stdout, r.stderr
    assert f"    {host} '<link" in p["CHAT_THEME_HTTP_CONF"].read_text()


def test_theme_host_length_is_capped(tmp_path):
    host = ".".join(["a" * 60] * 5)  # 304 characters
    r, _ = run_entrypoint(tmp_path, CHAT_THEMES=f"{host}=paper-tiger")
    assert "themes OFF" in r.stderr


def nginx_blocks(text: str) -> list[tuple[str, str]]:
    """(prefix, body) of each innermost `... { ... }` block."""
    return re.findall(r"([^{};]*)\{([^{}]*)\}", text)


def test_health_listener_serves_only_health(tmp_path):
    r, p = run_entrypoint(tmp_path)
    assert r.returncode == 0, r.stderr
    conf = p["CHAT_HEALTH_CONF"].read_text()
    assert "listen 8081 default_server;" in conf
    assert re.findall(r"listen\s+[^;]*;", conf) == ["listen 8081 default_server;"]
    assert "include" not in conf  # nothing of Zulip's site
    locations = {m.strip() for m in re.findall(r"location\s+([^{]+)\{", conf)}
    assert locations == {"= /health", "/"}
    body = conf[conf.index("location = /health"):conf.index("location / {")]
    # Re-made from 127.0.0.1 to Zulip's local server (which /health allows), fixed Host,
    # the caller's forwarding headers dropped.
    assert "proxy_pass http://127.0.0.1:80/health;" in body
    assert "proxy_set_header Host 127.0.0.1;" in body
    assert 'proxy_set_header X-Forwarded-For "";' in body
    assert 'proxy_set_header X-Real-IP "";' in body
    assert "limit_except GET HEAD { deny all; }" in body
    catch_all = conf[conf.index("location / {"):]
    assert re.match(r"location / \{\s*return 404;\s*\}", catch_all)
    assert "chat-entrypoint: health-only listener on :8081" in r.stdout


def test_health_listener_path_is_conf_d_by_default():
    text = (BIN / "chat-entrypoint").read_text()
    assert 'HEALTH_CONF="${CHAT_HEALTH_CONF:-/etc/nginx/conf.d/chat-health.conf}"' in text
    assert "HEALTH_PORT=8081" in text


# ---------------------------------------------------------------- chat-manage

FAKE_MODULES = {
    "django/__init__.py": "",
    "django/core/__init__.py": "",
    "django/core/management/__init__.py": textwrap.dedent('''
        from zerver.models import _state, _save
        def call_command(name, *args, **kw):
            st = _state(); st.setdefault("calls", []).append([name, list(args), kw]); _save(st)
    '''),
    "zerver/__init__.py": "",
    "zerver/models/__init__.py": textwrap.dedent('''
        import json, os
        def _state():
            with open(os.environ["FAKE_STATE"]) as f:
                return json.load(f)
        def _save(st):
            with open(os.environ["FAKE_STATE"], "w") as f:
                json.dump(st, f)

        class Realm:
            def __init__(self, string_id):
                self.string_id, self.url, self.deactivated = string_id, f"https://{string_id or 'root'}.test", False
            class _Objects:
                def get(self, string_id):
                    return Realm(string_id)
                def filter(self, string_id):
                    class F:
                        def first(_):
                            return Realm(string_id)
                    return F()
            objects = _Objects()

        class _User:
            def __init__(self, d):
                self.__dict__.update(d)

        class UserProfile:
            ROLE_REALM_OWNER, ROLE_REALM_ADMINISTRATOR, ROLE_MODERATOR, ROLE_MEMBER, ROLE_GUEST = 100, 200, 300, 400, 600
            class DoesNotExist(Exception):
                pass
            class _Objects:
                def filter(self, realm=None, **kw):
                    users = [u for u in _state()["users"] if all(u.get(k) == v for k, v in kw.items())]
                    class Q:
                        def count(_):
                            return len(users)
                    return Q()
            objects = _Objects()
    '''),
    "zerver/models/users.py": textwrap.dedent('''
        from zerver.models import UserProfile, _User, _state
        def get_user_by_delivery_email(email, realm):
            # Like Zulip's: no is_active filter.
            for u in _state()["users"]:
                if u["email"] == email:
                    return _User(u)
            raise UserProfile.DoesNotExist(email)
    '''),
    "zerver/actions/__init__.py": "",
    "zerver/actions/create_user.py": textwrap.dedent('''
        from zerver.models import _User, _state, _save
        def do_create_user(email, password, realm, full, role, acting_user):
            st = _state(); u = {"email": email, "role": role, "is_active": True, "is_bot": False}
            st["users"].append(u); _save(st)
            return _User(u)
    '''),
    "zerver/actions/users.py": textwrap.dedent('''
        from zerver.models import _state, _save
        def do_change_user_role(user, role, acting_user, notify):
            st = _state()
            for u in st["users"]:
                if u["email"] == user.email:
                    u["role"] = role
            _save(st)
    '''),
}

FAKE_MANAGE = '''#!{python}
import json, os, sys
sys.path.insert(0, os.environ["FAKE_MODULES"])
with open(os.environ["FAKE_MANAGE_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["shell", "--no-startup"]:
    exec(compile(sys.argv[4], "<shell>", "exec"), {{"__name__": "__main__"}})
    sys.exit(0)
sys.exit(int(os.environ.get("FAKE_MANAGE_RC", "0")))
'''

# su zulip -c "<command line>": run it the way su would, through a shell.
FAKE_SU = '''#!/bin/bash
[[ "$1" == zulip && "$2" == -c && $# -eq 3 ]] || { echo "unexpected su $*" >&2; exit 99; }
exec bash -c "$3"
'''


@pytest.fixture
def zulip(tmp_path):
    mods = tmp_path / "modules"
    for rel, text in FAKE_MODULES.items():
        (mods / rel).parent.mkdir(parents=True, exist_ok=True)
        (mods / rel).write_text(text)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable(bindir / "su", FAKE_SU)
    manage = executable(tmp_path / "manage.py", FAKE_MANAGE.format(python=sys.executable))
    entry = executable(tmp_path / "entrypoint.sh", "#!/bin/sh\n[ \"$1\" = app:init ] || exit 98\n")
    state = tmp_path / "state.json"
    log = tmp_path / "manage.log"

    class Z:
        def users(self, *users):
            state.write_text(json.dumps({"users": [dict(zip(("email", "role", "is_active", "is_bot"), u)) for u in users]}))

        def state(self):
            return json.loads(state.read_text())

        def calls(self):
            return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []

        def run(self, *args, rc=0):
            env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "CHAT_MANAGE_PY": str(manage),
                   "CHAT_ZULIP_ENTRYPOINT": str(entry), "FAKE_STATE": str(state), "FAKE_MODULES": str(mods),
                   "FAKE_MANAGE_LOG": str(log), "FAKE_MANAGE_RC": str(rc)}
            r = subprocess.run(["bash", str(BIN / "chat-manage"), *args], env=env, capture_output=True, text=True)
            r.results = results(r.stdout)
            return r

    z = Z()
    z.users()
    return z


def test_deactivate_realm_passes_a_reason(zulip):
    r = zulip.run("deactivate-realm", "ahmadi-group")
    assert r.returncode == 0, r.stderr
    assert zulip.calls() == [["deactivate_realm", "-r", "ahmadi-group", "--deactivation_reason", "owner_request"]]
    assert r.results == [{"ok": True, "slug": "ahmadi-group", "deactivated": "yes"}]


@pytest.mark.parametrize("args", [("deactivate-realm", "ahmadi-group"), ("register-push",),
                                  ("send-test-email", "someone@example.edu")])
def test_failing_manage_command_is_ok_false(zulip, args):
    r = zulip.run(*args, rc=1)
    assert r.returncode != 0
    assert r.results and r.results[-1]["ok"] is False
    assert not any(x["ok"] for x in r.results), r.stdout


def test_manage_arguments_are_quoted(zulip):
    r = zulip.run("send-test-email", "a'b;touch x@example.edu")
    assert r.returncode == 0, r.stderr
    assert zulip.calls() == [["send_test_email", "a'b;touch x@example.edu"]]


def test_owner_request_is_the_reason_zulip_12_accepts():
    # zerver/actions/realm_settings.py (12.3) lists the choices; owner_request is one.
    text = (BIN / "chat-manage").read_text()
    assert "--deactivation_reason owner_request" in text


OWNER, ADMIN, MEMBER = 100, 200, 400


def test_set_role_changes_an_active_member(zulip):
    zulip.users(("o@x.edu", OWNER, True, False), ("m@x.edu", MEMBER, True, False))
    r = zulip.run("set-role", "_root", "m@x.edu", "admin")
    assert r.returncode == 0, r.stderr
    assert r.results[-1] == {"ok": True, "email": "m@x.edu", "role": "admin", "created": False, "realm": "https://root.test"}
    assert zulip.state()["users"][1]["role"] == ADMIN


def test_set_role_refuses_a_deactivated_account(zulip):
    zulip.users(("o@x.edu", OWNER, True, False), ("gone@x.edu", MEMBER, False, False))
    r = zulip.run("set-role", "_root", "gone@x.edu", "admin")
    assert r.returncode != 0
    assert r.results[-1]["ok"] is False and "deactivated" in r.results[-1]["error"]
    assert not any(x["ok"] for x in r.results)
    assert zulip.state()["users"][1]["role"] == MEMBER


def test_set_role_refuses_to_demote_the_last_active_owner(zulip):
    # An inactive or bot owner does not count.
    zulip.users(("o@x.edu", OWNER, True, False), ("old@x.edu", OWNER, False, False), ("bot@x.edu", OWNER, True, True))
    r = zulip.run("set-role", "_root", "o@x.edu", "admin")
    assert r.returncode != 0
    assert r.results[-1]["ok"] is False and "last active owner" in r.results[-1]["error"]
    assert zulip.state()["users"][0]["role"] == OWNER


def test_set_role_demotes_an_owner_when_another_remains(zulip):
    zulip.users(("o@x.edu", OWNER, True, False), ("o2@x.edu", OWNER, True, False))
    r = zulip.run("set-role", "_root", "o@x.edu", "member")
    assert r.returncode == 0, r.stderr
    assert r.results[-1]["ok"] is True and zulip.state()["users"][0]["role"] == MEMBER


def test_set_role_owner_to_owner_is_a_no_op(zulip):
    zulip.users(("o@x.edu", OWNER, True, False))
    r = zulip.run("set-role", "_root", "o@x.edu", "owner")
    assert r.returncode == 0 and r.results[-1]["ok"] is True


def test_set_role_creates_with_full_name(zulip):
    r = zulip.run("set-role", "_root", "new@x.edu", "moderator", "New Person")
    assert r.returncode == 0, r.stderr
    assert r.results[-1]["created"] is True
    assert zulip.state()["users"] == [{"email": "new@x.edu", "role": 300, "is_active": True, "is_bot": False}]


def test_shell_crash_is_ok_false(zulip, tmp_path):
    (tmp_path / "state.json").write_text("not json")  # every fake model call now raises
    r = zulip.run("set-role", "_root", "o@x.edu", "admin")
    assert r.returncode != 0
    assert r.results and r.results[-1]["ok"] is False and not any(x["ok"] for x in r.results)


def test_failed_init_is_ok_false(zulip, tmp_path):
    executable(tmp_path / "entrypoint.sh", "#!/bin/sh\nexit 1\n")
    r = zulip.run("list-realms")
    assert r.returncode != 0 and r.results == [{"ok": False, "error": "app:init failed"}]
    assert zulip.calls() == []


# ---------------------------------------------------------------- chat-dbinit

# psql: log argv and stdin; answer the collation query.
FAKE_PSQL = '''#!/bin/sh
{ printf 'ARGV:'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'; cat; printf '\\n--END--\\n'; } >> "$PSQL_LOG"
case "$*" in *-At*) printf '%s\\n' "${FAKE_COLLATE-C.UTF-8}" ;; esac
'''


def run_dbinit(tmp_path, name="zulip_dept", user=None, action="ensure", **env):
    bindir = tmp_path / "pgbin"
    bindir.mkdir(exist_ok=True)
    executable(bindir / "psql", FAKE_PSQL)
    (tmp_path / "admin").write_text("admin-pw")
    (tmp_path / "db").write_text("db-pw")
    log = tmp_path / "psql.log"
    full = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "PSQL_LOG": str(log), "PGHOST": "pg",
            "PGUSER": "chatadmin", "PGADMIN_PASSWORD_FILE": str(tmp_path / "admin"), "DB_NAME": name,
            "DB_USER": name if user is None else user, "DB_PASSWORD_FILE": str(tmp_path / "db"),
            "DB_ACTION": action, **env}
    r = subprocess.run(["sh", str(BIN / "chat-dbinit")], env=full, capture_output=True, text=True)
    return r, (log.read_text() if log.exists() else "")


@pytest.mark.parametrize("name,user", [
    ("x'; DROP DATABASE postgres; --", None),
    ("zulip_dept", "Zulip"),
    ("zulip-dept", None),
    ("1zulip", None),
    ("a" * 64, None),
    ('zulip"dept', None),
    ("zulip_dept\nother", None),
    ("zulip dept", None),
])
def test_bad_identifiers_refused_before_psql(tmp_path, name, user):
    r, log = run_dbinit(tmp_path, name, user)
    assert r.returncode != 0
    (res,) = results(r.stdout)  # still valid JSON
    assert res["ok"] is False and "DB_NAME and DB_USER" in res["error"]
    assert log == ""


def test_longest_valid_identifier_accepted(tmp_path):
    r, _ = run_dbinit(tmp_path, "_" + "a" * 62)
    assert r.returncode == 0, r.stdout + r.stderr


def test_ensure_never_interpolates_names_into_sql(tmp_path):
    r, log = run_dbinit(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert results(r.stdout) == [{"ok": True, "database": "zulip_dept", "collation": "C.UTF-8"}]
    calls = log.split("--END--\n")[:-1]
    assert len(calls) == 2
    for call in calls:
        argv, _, sql = call.partition("\n")
        assert "-c]" not in argv  # SQL only ever on stdin...
        assert "zulip_dept" not in sql  # ...and names only as psql variables
        assert "[ON_ERROR_STOP=1]" in argv and "[dbname=zulip_dept]" in argv
    assert "WHERE datname = :'dbname'" in calls[1]


def test_ensure_revokes_public_access_as_the_owner(tmp_path):
    _, log = run_dbinit(tmp_path)
    sql = log.split("--END--\n")[0]
    lines = [l.strip() for l in sql.splitlines()]
    alter = next(i for i, l in enumerate(lines) if l.startswith("SELECT format('ALTER DATABASE %I OWNER TO %I'"))
    revoke = lines.index("SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'dbname') \\gexec")
    set_role = lines.index("SELECT format('SET ROLE %I', :'dbuser') \\gexec")
    assert alter < set_role < revoke < lines.index("RESET ROLE;")


def test_odd_collation_keeps_the_result_valid_json(tmp_path):
    r, _ = run_dbinit(tmp_path, FAKE_COLLATE='en_US"}, "ok": true, "x": "')
    assert r.returncode != 0
    (res,) = results(r.stdout)
    assert res["ok"] is False and "Zulip needs C.UTF-8" in res["error"]


def test_drop_validates_too(tmp_path):
    r, log = run_dbinit(tmp_path, "x;y", action="drop")
    assert r.returncode != 0 and results(r.stdout)[0]["ok"] is False and log == ""


# ---------------------------------------------------------------- render -> bicep


def bicep_params() -> set[str]:
    return set(re.findall(r"^param (\w+) ", (ROOT / "infra" / "server.bicep").read_text(), flags=re.M))


def test_hc_identity_param_exists_and_defaults_to_disabled():
    text = (ROOT / "infra" / "server.bicep").read_text()
    assert "param hcIdentityName string = ''" in text
    assert "var hcEnabled = healthchecksEnabled && !empty(hcIdentityName)" in text


def test_every_resolved_parameter_is_a_server_bicep_parameter(dept, capsys):
    import render
    assert render.main(["resolve", "--server", str(dept.path / "generated/servers/dept.json"),
                        "--default-domain", "x.azurecontainerapps.io", "--image",
                        "orfechatacr.azurecr.io/chat@sha256:" + "0" * 64,
                        "--registry", "orfechatacr.azurecr.io"]) == 0
    params = set(json.loads(capsys.readouterr().out)["parameters"])
    assert params, "resolve produced no parameters"
    assert params <= bicep_params(), params - bicep_params()
