"""image/bin/chat-dbinit against a real PostgreSQL 17 whose admin, like Azure's, is not
a superuser. Runs in the docker-compose test harness (PGHOST=postgres)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "image" / "bin" / "chat-dbinit"

if not os.environ.get("CHAT_TEST_PGHOST"):
    if os.environ.get("CHAT_REQUIRE_INTEGRATION") == "1":
        raise RuntimeError("CHAT_TEST_PGHOST not set but integration tests are required")
    pytest.skip("no PostgreSQL (set CHAT_TEST_PGHOST; the compose harness does)", allow_module_level=True)


def psql(sql: str, *, user="chatadmin", password="admin-test-password", db="postgres") -> str:
    env = {**os.environ, "PGHOST": os.environ["CHAT_TEST_PGHOST"], "PGUSER": user,
           "PGPASSWORD": password, "PGDATABASE": db, "PGSSLMODE": "disable"}
    return subprocess.run(["psql", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql], env=env,
                          capture_output=True, text=True, check=True).stdout.strip()


def dbinit(tmp_path, name: str, password: str, action="ensure", admin_password="admin-test-password"):
    (tmp_path / "admin").write_text(admin_password)
    (tmp_path / "db").write_text(password)
    env = {**os.environ, "PGHOST": os.environ["CHAT_TEST_PGHOST"], "PGUSER": "chatadmin",
           "PGSSLMODE": "disable", "PGADMIN_PASSWORD_FILE": str(tmp_path / "admin"),
           "DB_NAME": name, "DB_USER": name, "DB_PASSWORD_FILE": str(tmp_path / "db"), "DB_ACTION": action}
    return subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)


@pytest.fixture(autouse=True, scope="module")
def fresh_databases(tmp_path_factory):
    """`docker-compose run --rm tests` leaves the postgres service up between runs; start
    every run from no test databases, or a leftover one makes the next run fail or pass
    for the wrong reason."""
    tmp = tmp_path_factory.mktemp("dbinit-reset")
    for i in range(1, 6):
        r = dbinit(tmp, f"zulip_t{i}", "reset", action="drop")
        assert r.returncode == 0, r.stderr + r.stdout


def test_admin_is_not_superuser():
    assert psql("SELECT rolsuper FROM pg_roles WHERE rolname = current_user") == "f"


def test_creates_c_utf8_database_owned_by_its_role(tmp_path):
    r = dbinit(tmp_path, "zulip_t1", "first-password")
    assert r.returncode == 0, r.stderr + r.stdout
    assert 'CHAT-RESULT: {"ok": true, "database": "zulip_t1", "collation": "C.UTF-8"}' in r.stdout
    row = psql("SELECT datcollate, datctype, pg_get_userbyid(datdba), pg_encoding_to_char(encoding) "
               "FROM pg_database WHERE datname = 'zulip_t1'")
    assert row == "C.UTF-8|C.UTF-8|zulip_t1|UTF8"
    # The Zulip role can log in with its password and owns its schema space.
    assert psql("SELECT current_user", user="zulip_t1", password="first-password", db="zulip_t1") == "zulip_t1"
    psql("CREATE TABLE probe (x int); DROP TABLE probe;", user="zulip_t1", password="first-password", db="zulip_t1")


def test_rerun_is_idempotent_and_updates_the_password(tmp_path):
    assert dbinit(tmp_path, "zulip_t2", "old").returncode == 0
    psql("CREATE TABLE keepme (x int)", user="zulip_t2", password="old", db="zulip_t2")
    r = dbinit(tmp_path, "zulip_t2", "new")
    assert r.returncode == 0, r.stderr + r.stdout
    assert psql("SELECT count(*) FROM pg_tables WHERE tablename = 'keepme'", user="zulip_t2", password="new", db="zulip_t2") == "1"
    with pytest.raises(subprocess.CalledProcessError):
        psql("SELECT 1", user="zulip_t2", password="old", db="zulip_t2")


def test_wrong_admin_password_fails_loudly(tmp_path):
    r = dbinit(tmp_path, "zulip_t3", "pw", admin_password="wrong")
    assert r.returncode != 0
    assert '"ok": true' not in r.stdout


def test_empty_password_refused(tmp_path):
    r = dbinit(tmp_path, "zulip_t4", "")
    assert r.returncode != 0 and "empty database password" in r.stdout


def test_drop_removes_database_and_role(tmp_path):
    assert dbinit(tmp_path, "zulip_t5", "pw").returncode == 0
    r = dbinit(tmp_path, "zulip_t5", "pw", action="drop")
    assert r.returncode == 0, r.stderr + r.stdout
    assert psql("SELECT count(*) FROM pg_database WHERE datname = 'zulip_t5'") == "0"
    assert psql("SELECT count(*) FROM pg_roles WHERE rolname = 'zulip_t5'") == "0"
