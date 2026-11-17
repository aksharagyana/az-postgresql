#!/usr/bin/env python3
"""Offline logical backup, fresh-server restore, and full-content comparison."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import BinaryIO, Iterator


class BackupError(RuntimeError):
    pass


MANAGED_DATABASES = {"azure_sys", "azure_maintenance", "template0", "template1"}
KNOWN_AZURE_ROLES = {"azure_pg_admin", "azure_superuser"}
SYSTEM_SCHEMA = "n.nspname = 'information_schema' OR n.nspname ~ '^pg_'"
SESSION_OPTIONS = (
    "-c timezone=UTC -c datestyle=ISO,YMD -c intervalstyle=postgres "
    "-c extra_float_digits=3 -c bytea_output=hex -c row_security=off "
    "-c search_path=pg_catalog -c standard_conforming_strings=on"
)


def managed_role(name: str) -> bool:
    return name.startswith(("pg_", "azure_")) or name in {"azuresu", "replication"}


def ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_json(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=True) + "\n")


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(
    command: list[str], *, env: dict[str, str] | None = None,
    output: BinaryIO | None = None, source: BinaryIO | None = None,
    reject_stderr: bool = False,
) -> bytes:
    result = subprocess.run(
        command, env=env, stdin=source, stdout=output or subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode or (reject_stderr and stderr.strip()):
        raise BackupError(f"{Path(command[0]).name} failed ({result.returncode}):\n{stderr}")
    if stderr:
        print(stderr, file=sys.stderr, end="")
    return result.stdout or b""


@dataclass
class Server:
    host: str
    user: str
    port: int = 5432
    bin_dir: Path | None = None
    schema_bin_dir: Path | None = None

    def binary(self, tool: str, *, schema: bool = False) -> str:
        directory = self.schema_bin_dir if schema else self.bin_dir
        name = str(directory / tool) if directory else tool
        found = shutil.which(name)
        if not found:
            raise BackupError(f"Missing {name}; install the documented PostgreSQL clients")
        return found

    def connection(self, database: str) -> str:
        def quote(value: str) -> str:
            return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        return " ".join(
            f"{key}={quote(str(value))}"
            for key, value in (
                ("host", self.host), ("port", self.port),
                ("user", self.user), ("dbname", database),
            )
        )

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            PGOPTIONS=SESSION_OPTIONS, PGCLIENTENCODING="UTF8",
            PGAPPNAME="azure-postgresql-offline-backup",
            PGCONNECT_TIMEOUT="15",
        )
        env.setdefault("PGSSLMODE", "verify-full")
        return env

    def command(self, tool: str, database: str, *args: str, schema: bool = False) -> list[str]:
        return [
            self.binary(tool, schema=schema), "--no-password",
            "--dbname", self.connection(database), *args,
        ]

    def sql(self, database: str, sql: str) -> str:
        return run(
            self.command("psql", database, "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-c", sql),
            env=self.environment(),
        ).decode("utf-8").strip()

    def rows(self, database: str, sql: str) -> list[dict]:
        return json.loads(self.sql(database, f"""
            SELECT COALESCE(json_agg(row_to_json(q)), '[]'::json)
            FROM ({sql}) q
        """))

    def copy_hash(self, database: str, query: str) -> dict:
        command = self.command(
            "psql", database, "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
            "-c", f"COPY ({query}) TO STDOUT",
        )
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=errors, env=self.environment(),
            ) as process:
                assert process.stdout is not None
                try:
                    result = hash_rows(process.stdout)
                except (KeyboardInterrupt, OSError, BackupError):
                    process.terminate()
                    process.wait()
                    raise
                code = process.wait()
                errors.seek(0)
                stderr = errors.read().decode("utf-8", errors="replace")
                if code or stderr.strip():
                    raise BackupError(f"Full-data scan failed ({code}): {stderr}")
        return result

    def tool_major(self, tool: str, *, schema: bool = False) -> int:
        version = run([self.binary(tool, schema=schema), "--version"]).decode()
        match = re.search(r"(\d+)\.\d+", version)
        if not match:
            raise BackupError(f"Cannot determine {tool} version: {version}")
        return int(match[1])

    def major(self) -> int:
        return int(self.sql("postgres", "SHOW server_version_num")) // 10000

    def check_clients(self, operation: str, dump_major: int | None = None) -> None:
        if self.tool_major("pg_dump", schema=True) != 18:
            raise BackupError("Schema comparison requires PostgreSQL 18 pg_dump on every run")
        if self.tool_major("psql") < 16:
            raise BackupError("psql 16 or newer is required")
        major = self.major()
        if major not in (16, 18):
            raise BackupError("This migration tool supports PostgreSQL 16 and 18 only")
        if operation == "backup" and self.tool_major("pg_dump") != major:
            raise BackupError("Back up with pg_dump matching the source major (16 for rollback)")
        if operation == "restore":
            if self.tool_major("pg_restore") != major:
                raise BackupError("Use pg_restore matching the target major")
            if dump_major is None or major < dump_major:
                raise BackupError("Restoring a newer-major dump into an older server is unsupported")


def hash_rows(rows: BinaryIO) -> dict:
    """Hash a sorted multiset of row hashes; disk-backed sort preserves duplicates."""
    count = 0
    with tempfile.TemporaryDirectory(prefix="pg-integrity-") as directory:
        root = Path(directory)
        hashes = root / "rows"
        with hashes.open("wb") as stream:
            for row in rows:
                stream.write(hashlib.sha256(row).hexdigest().encode("ascii") + b"\n")
                count += 1
        sorted_hashes = root / "sorted"
        with sorted_hashes.open("wb") as output:
            run(
                ["sort", "-T", directory, str(hashes)],
                env={**os.environ, "LC_ALL": "C"}, output=output,
            )
        return {"rows": count, "sha256": file_hash(sorted_hashes)}


def role_sql(role: dict) -> str:
    name = role["name"]
    if managed_role(name):
        raise BackupError(f"Refusing to modify managed role {name!r}")
    attributes = [
        "LOGIN" if role["login"] else "NOLOGIN",
        "INHERIT" if role["inherit"] else "NOINHERIT",
        "CREATEDB" if role["createdb"] else "NOCREATEDB",
        "CREATEROLE" if role["createrole"] else "NOCREATEROLE",
        f"CONNECTION LIMIT {int(role['connection_limit'])}",
    ]
    if role["valid_until"] is not None:
        attributes.append("VALID UNTIL " + literal(role["valid_until"]))
    sql = f"CREATE ROLE {ident(name)} {' '.join(attributes)};\n"
    for setting in role["config"] or []:
        key, value = setting.split("=", 1)
        sql += f"ALTER ROLE {ident(name)} SET {ident(key)} TO {literal(value)};\n"
    if role["comment"] is not None:
        sql += f"COMMENT ON ROLE {ident(name)} IS {literal(role['comment'])};\n"
    return sql


def global_state(server: Server) -> dict:
    roles = server.rows("postgres", """
        SELECT rolname AS name, rolcanlogin AS login, rolinherit AS inherit,
               rolcreatedb AS createdb, rolcreaterole AS createrole,
               rolconnlimit AS connection_limit, rolvaliduntil::text AS valid_until,
               rolconfig AS config, shobj_description(oid, 'pg_authid') AS comment,
               rolsuper AS superuser, rolreplication AS replication, rolbypassrls AS bypassrls
        FROM pg_roles ORDER BY rolname COLLATE "C"
    """)
    custom = []
    for role in roles:
        if (
            role["name"].startswith("azure_") and role["name"] not in KNOWN_AZURE_ROLES
            and role["name"] != server.user
        ):
            raise BackupError(
                f"Unrecognized azure_-prefixed role {role['name']!r}; "
                "review whether it is managed before extending the protected role list"
            )
        if managed_role(role["name"]) or role["name"] == server.user:
            continue
        if any(role[key] for key in ("superuser", "replication", "bypassrls")):
            raise BackupError(f"Role {role['name']!r} has nonportable privileged attributes")
        custom.append(role)
    names = {role["name"] for role in custom}
    memberships = server.rows("postgres", """
        SELECT r.rolname AS role, m.rolname AS member, g.rolname AS grantor,
               a.admin_option, a.inherit_option, a.set_option
        FROM pg_auth_members a
        JOIN pg_roles r ON r.oid = a.roleid
        JOIN pg_roles m ON m.oid = a.member
        JOIN pg_roles g ON g.oid = a.grantor
        ORDER BY r.rolname COLLATE "C", m.rolname COLLATE "C", g.rolname COLLATE "C"
    """)
    labels = server.rows("postgres", """
        SELECT r.rolname AS name, l.provider, l.label FROM pg_shseclabel l
        JOIN pg_roles r ON r.oid = l.objoid
        WHERE l.classoid = 'pg_authid'::regclass
    """)
    if any(label["name"] in names for label in labels):
        raise BackupError("Entra/security-labelled custom roles require a separate reviewed migration")
    return {
        "roles": custom,
        "memberships": [
            row for row in memberships if row["role"] in names or row["member"] in names
        ],
    }


def databases(server: Server) -> list[dict]:
    result = server.rows("postgres", """
        SELECT datname AS name, datallowconn AS connect, datistemplate AS template,
               pg_get_userbyid(datdba) AS owner, pg_encoding_to_char(encoding) AS encoding,
               datcollate AS collate, datctype AS ctype, datlocprovider AS provider,
               COALESCE(to_jsonb(d)->>'datlocale', to_jsonb(d)->>'daticulocale') AS locale,
               datconnlimit AS connection_limit, datacl::text AS acl,
               shobj_description(oid, 'pg_database') AS comment
        FROM pg_database d ORDER BY datname COLLATE "C"
    """)
    for db in result:
        if db["name"] not in MANAGED_DATABASES and (not db["connect"] or db["template"]):
            raise BackupError(f"Custom template/inaccessible database requires manual handling: {db['name']!r}")
    return [db for db in result if db["name"] not in MANAGED_DATABASES]


def user_object_count(server: Server, database: str) -> int:
    return int(server.sql(database, f"""
        SELECT
          (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE NOT ({SYSTEM_SCHEMA})) +
          (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
           WHERE NOT ({SYSTEM_SCHEMA})) +
          (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
           WHERE NOT ({SYSTEM_SCHEMA})) +
          (SELECT count(*) FROM pg_namespace n
           WHERE NOT ({SYSTEM_SCHEMA}) AND n.nspname <> 'public') +
          (SELECT count(*) FROM pg_extension WHERE extname <> 'plpgsql') +
          (SELECT count(*) FROM pg_event_trigger) +
          (SELECT count(*) FROM pg_foreign_server) +
          (SELECT count(*) FROM pg_largeobject_metadata)
    """))


def preflight(server: Server) -> list[dict]:
    if server.sql("postgres", "SELECT pg_is_in_recovery()") != "f":
        raise BackupError("Connect directly to the primary server, not a replica or pooler")
    if server.sql("postgres", "SELECT count(*) FROM pg_prepared_xacts") != "0":
        raise BackupError("Resolve prepared transactions before taking the offline backup")
    if server.sql("postgres", "SELECT count(*) FROM pg_replication_slots WHERE slot_type='logical'") != "0":
        raise BackupError("Logical replication slots require a separate migration plan; none are restored")
    if server.sql("postgres", """
        SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default', 'pg_global')
    """) != "0":
        raise BackupError("Custom tablespaces are not supported by this Azure logical migration")
    if user_object_count(server, "template1"):
        raise BackupError("template1 contains non-default objects; migrate these explicitly first")
    inspect_azure_sys = server.sql("postgres", """
        SELECT EXISTS (
            SELECT 1 FROM pg_database
            WHERE datname='azure_sys' AND has_database_privilege(oid, 'CONNECT')
        )
    """)
    if inspect_azure_sys == "t":
        if server.sql("azure_sys", "SELECT to_regclass('cron.job') IS NOT NULL") == "t":
            if server.sql("azure_sys", "SELECT count(*) FROM cron.job") != "0":
                raise BackupError(
                    "pg_cron jobs in azure_sys require a separate reviewed export/restore; "
                    "the managed database must not be dumped or replaced"
                )
    dbs = databases(server)
    for db in dbs:
        name = db["name"]
        if server.sql(name, "SELECT count(*) FROM pg_subscription") != "0":
            raise BackupError(f"{name!r}: subscriptions require a separate replication migration")
        if server.sql(name, "SELECT count(*) FROM pg_foreign_table") != "0":
            raise BackupError(f"{name!r}: foreign table data is external and cannot be backed up here")
        managed = server.rows(name, f"""
            SELECT n.nspname, r.rolname
            FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
            WHERE NOT ({SYSTEM_SCHEMA}) AND n.nspname <> 'public'
        """)
        if any(managed_role(row["rolname"]) for row in managed):
            raise BackupError(f"{name!r}: managed-owned non-system schemas need a reviewed exclusion plan")
        extensions = server.rows(name, """
            SELECT e.extname, r.rolname FROM pg_extension e
            JOIN pg_roles r ON r.oid=e.extowner WHERE e.extname <> 'plpgsql'
        """)
        if any(managed_role(row["rolname"]) for row in extensions):
            raise BackupError(f"{name!r}: managed-owned extensions require a reviewed migration plan")
        objects = server.rows(name, f"""
            SELECT c.relname AS name, r.rolname AS owner
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_roles r ON r.oid=c.relowner WHERE NOT ({SYSTEM_SCHEMA})
            UNION ALL
            SELECT p.proname, r.rolname
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            JOIN pg_roles r ON r.oid=p.proowner WHERE NOT ({SYSTEM_SCHEMA})
            UNION ALL
            SELECT t.typname, r.rolname
            FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
            JOIN pg_roles r ON r.oid=t.typowner WHERE NOT ({SYSTEM_SCHEMA})
        """)
        if any(managed_role(row["owner"]) for row in objects):
            raise BackupError(f"{name!r}: managed-owned non-system objects need a reviewed migration plan")
        active = server.rows(name, """
            SELECT usename, application_name FROM pg_stat_activity
            WHERE datname=current_database() AND pid<>pg_backend_pid()
              AND backend_type='client backend' AND state='active'
        """)
        if any(not managed_role(row["usename"] or "") for row in active):
            raise BackupError(f"{name!r}: other active sessions detected; stop applications and jobs")
    return dbs


def normalize_schema(data: bytes) -> bytes:
    lines = data.decode("utf-8").splitlines(keepends=True)
    for index in range(min(20, len(lines))):
        if lines[index].startswith(("-- Dumped from database version ", "-- Dumped by pg_dump version ")):
            lines[index] = ""
    if len(lines) > 4 and lines[4].startswith("\\restrict "):
        key = lines[4].strip().split(" ", 1)[1]
        for index, line in enumerate(lines):
            if line in (f"\\restrict {key}\n", f"\\unrestrict {key}\n"):
                lines[index] = line.replace(key, "NORMALIZED")
    return "".join(lines).encode("utf-8")


def snapshot_database(server: Server, db: dict) -> dict:
    name = db["name"]
    print(f"Scanning all schema and data: {name!r}", file=sys.stderr)
    schema = run(
        server.command(
            "pg_dump", name, "--schema-only", "--quote-all-identifiers",
            "--no-tablespaces", *([] if name == "postgres" else ["--create"]),
            schema=True,
        ),
        env=server.environment(), reject_stderr=True,
    )
    relations = server.rows(name, f"""
        SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
               c.relispopulated AS populated
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE NOT ({SYSTEM_SCHEMA}) AND c.relkind IN ('r', 'm', 'S')
        ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"
    """)
    data = {}
    for relation in relations:
        qualified = ident(relation["schema"]) + "." + ident(relation["name"])
        if relation["kind"] == "m" and not relation["populated"]:
            data[qualified] = {"unpopulated": True}
        elif relation["kind"] == "S":
            data[qualified] = server.rows(name, f"SELECT last_value, is_called FROM {qualified}")
        else:
            data[qualified] = server.copy_hash(
                name, f"SELECT row_to_json(t)::text FROM ONLY {qualified} t",
            )
    large_objects = server.copy_hash(name, """
        WITH RECURSIVE chunks(oid, pos, data) AS (
            SELECT oid, 0::bigint, lo_get(oid, 0, 65536) FROM pg_largeobject_metadata
            UNION ALL
            SELECT oid, pos+65536, lo_get(oid, pos+65536, 65536)
            FROM chunks WHERE octet_length(data)=65536
        )
        SELECT row_to_json(q)::text FROM (
            SELECT oid, pos, encode(data, 'hex') AS data FROM chunks
        ) q
    """)
    metadata = server.rows(name, """
        SELECT oid, pg_get_userbyid(lomowner) AS owner, lomacl::text AS acl
        FROM pg_largeobject_metadata ORDER BY oid
    """)
    extensions = server.rows(name, """
        SELECT extname, extversion FROM pg_extension ORDER BY extname COLLATE "C"
    """)
    settings = server.rows(name, """
        SELECT CASE WHEN setrole=0 THEN '' ELSE pg_get_userbyid(setrole) END AS role,
               setconfig AS config
        FROM pg_db_role_setting
        WHERE setdatabase=(SELECT oid FROM pg_database WHERE datname=current_database())
        ORDER BY 1
    """)
    public_acl = server.rows(name, """
        SELECT pg_get_userbyid(a.grantor) AS grantor,
               CASE WHEN a.grantee=0 THEN NULL ELSE pg_get_userbyid(a.grantee) END AS grantee,
               a.privilege_type AS privilege, a.is_grantable
        FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a
        WHERE n.nspname='public'
        ORDER BY 1, 2, 3
    """)
    return {
        "database": db, "schema_sha256": hashlib.sha256(normalize_schema(schema)).hexdigest(),
        "relations": data, "large_objects": large_objects,
        "large_object_metadata": metadata, "extensions": extensions, "settings": settings,
        "public_schema_acl": public_acl,
    }


def snapshot(server: Server) -> dict:
    dbs = preflight(server)
    return {
        "globals": global_state(server),
        "databases": {db["name"]: snapshot_database(server, db) for db in dbs},
    }


def safe_toc(toc: str, database: str) -> str:
    public = r'(?:"public"|public)'
    plpgsql = r'(?:"plpgsql"|plpgsql)'
    postgres = r'(?:"postgres"|postgres)'
    protected = [
        rf"SCHEMA - {public}", rf"COMMENT - SCHEMA {public}",
        rf"ACL (?:-|{public}) SCHEMA {public}",
        rf"EXTENSION - {plpgsql}", rf"COMMENT - EXTENSION {plpgsql}",
    ]
    if database == "postgres":
        protected += [
            rf"DATABASE - {postgres}", rf"DATABASE PROPERTIES - {postgres}",
            rf"COMMENT - DATABASE {postgres}", rf"ACL - DATABASE {postgres}",
        ]
    pattern = re.compile(r"^\d+; \d+ \d+ (?:" + "|".join(protected) + r")(?: |$)")
    return "".join(
        (";" if pattern.match(line) else "") + line
        for line in toc.splitlines(keepends=True)
    )


def database_toc(toc: str, *, metadata: bool) -> str:
    pattern = re.compile(
        r"^\d+; \d+ \d+ (?:DATABASE - |DATABASE PROPERTIES - |COMMENT - DATABASE |ACL - DATABASE )"
    )
    return "".join(
        (";" if not line.startswith(";") and bool(pattern.match(line)) != metadata else "") + line
        for line in toc.splitlines(keepends=True)
    )


def load_backup(root: Path) -> dict:
    manifest_path = root / "manifest.json"
    if file_hash(manifest_path) != (root / "manifest.sha256").read_text().strip():
        raise BackupError("Manifest checksum mismatch; do not restore this backup")
    manifest = json.loads(manifest_path.read_text())
    if manifest["format"] != 1:
        raise BackupError("Unsupported backup manifest format")
    for name, expected in manifest["files"].items():
        path = root / name
        if Path(name).name != name or path.is_symlink() or not path.is_file():
            raise BackupError(f"Unsafe or missing backup file path: {name!r}")
        if file_hash(path) != expected:
            raise BackupError(f"Backup file checksum mismatch: {name!r}")
    if "archives" in manifest:
        if set(manifest["archives"]) != set(manifest["baseline"]["databases"]):
            raise BackupError("Archive/database inventory mismatch")
        for name in manifest["archives"].values():
            if name not in manifest["files"]:
                raise BackupError(f"Archive path is not checksummed: {name!r}")
    return manifest


def backup_server(server: Server, root: Path) -> None:
    server.check_clients("backup")
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    baseline = snapshot(server)
    archives = {}
    for number, name in enumerate(baseline["databases"]):
        filename = f"database-{number:04d}.dump"
        print(f"Backing up: {name!r}", file=sys.stderr)
        with (root / filename).open("wb") as output:
            run(
                server.command("pg_dump", name, "--format=custom", "--quote-all-identifiers"),
                env=server.environment(), output=output, reject_stderr=True,
            )
        archives[name] = filename
    after = snapshot(server)
    if baseline != after:
        raise BackupError(
            "Server changed during backup; directory is incomplete. Quiesce all writers and retry\n"
            + "\n".join(differences(baseline, after))
        )
    roles = baseline["globals"]["roles"]
    (root / "roles.sql").write_text(
        "-- Custom roles only. Passwords and Entra identities are NOT included.\n"
        "SET standard_conforming_strings=on;\n" + "".join(role_sql(role) for role in roles)
    )
    manifest = {
        "format": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_host": server.host, "source_port": server.port, "admin": server.user,
        "source_major": server.major(), "schema_client_major": 18,
        "archives": archives, "baseline": baseline,
        "excluded_databases": sorted(MANAGED_DATABASES),
        "files": {name: file_hash(root / name) for name in [*archives.values(), "roles.sql"]},
    }
    write_json(root / "manifest.json", manifest)
    (root / "manifest.sha256").write_text(file_hash(root / "manifest.json") + "\n")
    load_backup(root)
    print(f"Backup complete and checksummed: {root}", file=sys.stderr)
    print(
        "Role passwords, Entra identities and Azure infrastructure are NOT backed up. "
        "Complete a restore rehearsal before upgrading.",
        file=sys.stderr,
    )


def differences(expected: object, actual: object, path: str = "") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        result = []
        for key in sorted(expected.keys() | actual.keys()):
            child = f"{path}/{key}"
            if key not in expected:
                result.append(f"Unexpected: {child}")
            elif key not in actual:
                result.append(f"Missing: {child}")
            else:
                result.extend(differences(expected[key], actual[key], child))
        return result
    return [] if expected == actual else [f"Changed: {path}"]


def validate_server(server: Server, root: Path, report: Path) -> None:
    manifest = load_backup(root)
    server.check_clients("validate")
    if server.user != manifest["admin"]:
        raise BackupError("Use the same administrator role name as the source; ownership is not remapped")
    actual = snapshot(server)
    mismatches = differences(manifest["baseline"], actual)
    write_json(report, {
        "passed": not mismatches, "target": server.host,
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "differences": mismatches, "actual": actual,
    })
    if mismatches:
        raise BackupError(
            f"Integrity validation FAILED ({len(mismatches)} differences); see {report}\n"
            + "\n".join(mismatches[:30])
        )
    print(f"Integrity validation PASSED: {report}", file=sys.stderr)


def portable_membership(row: dict, admin: str) -> bool:
    return (
        not managed_role(row["role"]) and row["role"] != admin
        and (not managed_role(row["member"]) or row["member"] == admin)
    )


def automatic_membership(row: dict, admin: str) -> bool:
    # CREATEROLE records the bootstrap superuser as grantor of this implicit grant.
    return (
        row["member"] == admin and managed_role(row["grantor"])
        and row["admin_option"] and not row["inherit_option"] and not row["set_option"]
    )


def restore_public_grants(server: Server, database: str, grants: list[dict]) -> None:
    pending = [
        grant for grant in grants
        if grant["grantee"] is not None
        and grant["grantee"] != server.user and not managed_role(grant["grantee"])
    ]
    while pending:
        progress = False
        for grant in pending[:]:
            privilege = grant["privilege"]
            if privilege not in ("CREATE", "USAGE"):
                raise BackupError("Unexpected public-schema privilege in backup")
            can_grant = server.sql(database, (
                f"SELECT has_schema_privilege({literal(grant['grantor'])}, "
                f"'public', '{privilege} WITH GRANT OPTION')"
            ))
            if can_grant != "t":
                continue
            server.sql(database, (
                f"SET ROLE {ident(grant['grantor'])}; "
                f"GRANT {privilege} ON SCHEMA public TO {ident(grant['grantee'])}"
                + (" WITH GRANT OPTION" if grant["is_grantable"] else "")
                + "; RESET ROLE"
            ))
            pending.remove(grant)
            progress = True
        if not progress:
            raise BackupError(f"{database!r}: cannot safely replay public-schema grant chain")


def restore_server(server: Server, root: Path, report: Path, confirm_target: str) -> None:
    if report.exists() or report.is_symlink():
        raise BackupError("Report path already exists; choose a new path before restoring")
    manifest = load_backup(root)
    if confirm_target != server.host:
        raise BackupError("--confirm-target must exactly match --host")
    if server.user != manifest["admin"]:
        raise BackupError("Provision the target with the same administrator role name as the source")
    if server.host == manifest["source_host"] and server.port == manifest["source_port"]:
        raise BackupError("Refusing to restore onto the recorded source endpoint")
    server.check_clients("restore", manifest["source_major"])
    dbs = preflight(server)
    if any(db["name"] != "postgres" for db in dbs) or user_object_count(server, "postgres"):
        raise BackupError("Restore requires a fresh target: application databases/objects already exist")
    existing = global_state(server)
    expected = manifest["baseline"]["globals"]
    existing_roles = {role["name"]: role for role in existing["roles"]}
    source_roles = {role["name"]: role for role in expected["roles"]}
    if existing_roles.keys() - source_roles.keys():
        raise BackupError("Target has additional custom roles; use a fresh, reviewed target")
    for name, role in existing_roles.items():
        if role != source_roles[name]:
            raise BackupError(f"Existing role {name!r} differs; refusing to alter it")
    for row in expected["memberships"]:
        if automatic_membership(row, server.user):
            if row["role"] in existing_roles and row not in existing["memberships"]:
                raise BackupError(f"Existing role lacks its expected automatic membership: {row!r}")
            continue
        if not portable_membership(row, server.user) and row not in existing["memberships"]:
            raise BackupError(
                f"Provision protected membership manually before restore: {row!r}; "
                "managed roles are never modified by this script"
            )
        if portable_membership(row, server.user) and row["grantor"] != server.user:
            raise BackupError(f"Membership has a different grantor; requires a reviewed migration: {row!r}")
    with tempfile.TemporaryDirectory(prefix="pg-restore-") as directory:
        work = Path(directory)
        lists = {}
        for number, (name, filename) in enumerate(manifest["archives"].items()):
            with (root / filename).open("rb") as source:
                toc = run(
                    [server.binary("pg_restore"), "--list"],
                    source=source, reject_stderr=True,
                ).decode("utf-8")
            safe = safe_toc(toc, name)
            metadata = work / f"{number}-database.list"
            objects = work / f"{number}-objects.list"
            metadata.write_text(database_toc(safe, metadata=True))
            objects.write_text(database_toc(safe, metadata=False))
            lists[name] = (metadata, objects)
        for name, role in source_roles.items():
            if name not in existing_roles:
                server.sql("postgres", "SET standard_conforming_strings=on;\n" + role_sql(role))
        for row in expected["memberships"]:
            if portable_membership(row, server.user) and not automatic_membership(row, server.user):
                options = ", ".join(
                    f"{key.upper().removesuffix('_OPTION')} {'TRUE' if row[key] else 'FALSE'}"
                    for key in ("admin_option", "inherit_option", "set_option")
                )
                server.sql("postgres", (
                    f"GRANT {ident(row['role'])} TO {ident(row['member'])} WITH {options} "
                    f"GRANTED BY {ident(row['grantor'])}"
                ))
        for name, filename in manifest["archives"].items():
            print(f"Restoring: {name!r}", file=sys.stderr)
            if name != "postgres":
                with (root / filename).open("rb") as source:
                    run(
                        server.command(
                            "pg_restore", "postgres", "--exit-on-error", "--create",
                            "--use-list", str(lists[name][0]),
                        ),
                        source=source, env=server.environment(), reject_stderr=True,
                    )
            restore_public_grants(
                server, name, manifest["baseline"]["databases"][name]["public_schema_acl"],
            )
            with (root / filename).open("rb") as source:
                run(
                    server.command(
                        "pg_restore", name, "--exit-on-error",
                        "--use-list", str(lists[name][1]),
                    ),
                    source=source, env=server.environment(), reject_stderr=True,
                )
    validate_server(server, root, report)
    print(
        "Restore verified. Keep traffic stopped until passwords/Entra, Azure configuration, "
        "statistics and application smoke tests are complete.",
        file=sys.stderr,
    )


@contextmanager
def private_files() -> Iterator[None]:
    mask = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(mask)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore", "validate"))
    parser.add_argument("--host", required=True, help="Direct server FQDN, not a PgBouncer endpoint")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", required=True, help="Server administrator; same name on both servers")
    parser.add_argument("--backup-dir", type=Path, required=True, help="New directory for backup; existing for restore/validate")
    parser.add_argument("--bin-dir", type=Path, help="Source-major clients for backup; target-major for restore")
    parser.add_argument("--schema-bin-dir", type=Path, help="PostgreSQL 18 pg_dump directory (also needed on source 16)")
    parser.add_argument("--quiesced", action="store_true", help="Confirm ALL writers, jobs and DDL are stopped")
    parser.add_argument("--confirm-target", help="Required for restore; repeat target hostname exactly")
    parser.add_argument("--report", type=Path, help="New JSON report path; required for restore/validate")
    args = parser.parse_args()
    if not args.quiesced:
        parser.error("--quiesced is mandatory; stop all traffic, background jobs, replication and DDL")
    if args.operation != "backup" and args.report is None:
        parser.error("--report is required for restore/validate")
    if args.operation == "restore" and args.confirm_target is None:
        parser.error("--confirm-target is required for restore")
    if args.report is not None and args.report.exists():
        parser.error("--report already exists; choose a new path")
    server = Server(args.host, args.user, args.port, args.bin_dir, args.schema_bin_dir)
    try:
        with private_files():
            if args.operation == "backup":
                backup_server(server, args.backup_dir)
            elif args.operation == "restore":
                restore_server(server, args.backup_dir, args.report, args.confirm_target)
            else:
                validate_server(server, args.backup_dir, args.report)
    except (BackupError, OSError, ValueError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if args.operation == "restore":
            print(
                "Restore is NOT complete. Keep traffic stopped; target may be partially populated. "
                "Inspect the error and use a new/reprovisioned target for a retry.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
