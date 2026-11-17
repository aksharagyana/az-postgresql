# Azure PostgreSQL 16 -> 18 offline recovery tools

Two independent workflows are available:

- **[Azure-managed PITR backup and restore](PITR.md)**: `azure_pitr.py` creates an
  on-demand Azure backup, saves a recovery reference, then can ask Azure to
  create a new restored server. Recommended primary upgrade rollback path.
  Run `make azure-login`, then `make pitr-backup` and `make pitr-restore`.
  Optional Entra-authenticated Blob upload/download stores the recovery-reference
  JSON only; the database backup remains in Azure-managed storage.
- **Logical backup and full-data comparison**, described below:
  `make backup`, `make restore`, and `make validate`.

`server_backup.py` provides reusable `backup_server`, `restore_server`, and
`validate_server` functions and a CLI. It does **not** upgrade or provision an
Azure server. Use the Azure portal for those operations.

The included Docker image is the recommended runtime: it contains Python, Make,
the PostgreSQL **16 and 18 clients side by side**, Azure CLI, and the scripts/tests. No
Homebrew, host PostgreSQL installation, Docker socket, or package installation
inside AKS is required. The same image runs on Linux Docker hosts, Docker Desktop,
and AKS Linux nodes. It contains **clients only**, not a database server.

**Keep every writer stopped from before backup until validation and application
smoke tests finish.** Blocking incoming HTTP traffic alone is insufficient:
stop workers, schedulers, `pg_cron`, ETL, database jobs, connection pools, DDL and
external integrations too. `--quiesced` is your explicit acknowledgement, not
a write lock or an automatic traffic switch.

## What is covered

- Every accessible non-system database, including empty databases and
  application objects/data in `postgres`. Database names are discovered, not
  supplied as an incomplete hand-maintained list.
- Native custom-format `pg_dump` archives: schemas, tables/partitions, data,
  indexes, constraints, views, materialized-view definitions, sequences,
  functions, procedures, triggers, types/domains, RLS policies, permissions,
  default privileges, comments and other objects supported by `pg_dump`.
- Custom role attributes, role settings/comments, and role memberships with
  PostgreSQL 16+ ADMIN/INHERIT/SET options. Ownership and grants are retained;
  neither `--no-owner` nor `--no-acl` is used.
- Large-object contents, original OIDs, ownership and ACLs.
- SHA-256 checksums for every archive, the role SQL, and the manifest.

### Azure defaults are not a portable backup

The script deliberately does **not** copy or recreate `azure_sys`,
`azure_maintenance`, `template0`, or `template1`. Never restore service-managed
databases manually. Accessible application databases must all be readable;
an inaccessible application database is an error, not a skipped database.

PostgreSQL `pg_*` roles and the known Azure roles `azuresu`, `azure_pg_admin`,
`azure_superuser`, and `replication` are protected. The login administrator is
also pre-provisioned, not recreated. Unknown `azure_`-prefixed roles cause an
error requiring review, rather than silently excluding a potentially custom
role. Managed-role memberships are checked but never replayed; provision any
required application memberships separately through an approved process.

Restore leaves the existing `postgres` database, native `public` schema
ownership/comments, and `plpgsql` extension alone. It does not replay the full
native `public` ACL: only grants to custom application roles are reconstructed,
in dependency order, preserving grantors. Default `PUBLIC`, managed-role and
administrator grants are left untouched. If your application deliberately
changed those defaults, public ownership/comments, or `postgres` database
properties/settings, reproduce the reviewed changes separately.
**Validation reports differences; it never waives them to manufacture a pass.**

Azure SQL extensions owned by managed roles, managed-owned objects outside
system schemas, Entra/security-labelled custom roles, privileged custom roles,
custom tablespaces/templates, foreign tables, subscriptions, logical replication
slots and prepared transactions require a separate reviewed migration plan.
Preflight stops instead of making a partial-success backup. Physical replication
slots are service infrastructure, not copied. Do not remove Azure-managed slots
or objects merely to get past a preflight error.

### Important exclusions and limits

- **Passwords cannot be exported from Azure Flexible Server.** Re-created LOGIN
  roles have no password. Reset them securely before reopening traffic. Passwords
  on matching pre-existing roles are not changed.
- Entra identities/configuration, firewall/VNet/private endpoints/DNS, Azure RBAC,
  TLS configuration, HA/replicas, backup policy, server parameters, extension
  allowlists/preload libraries, WAL/replication state and Azure infrastructure
  are not recreated. Record/provision these separately using your IaC/portal.
- Extension SQL is included by native tools, but binaries, external services,
  extension-internal state and foreign data are not universally portable.
  Extension-managed tables may require extension-specific backup instructions.
  Configure supported extensions on the target first; do not pre-create
  application database objects. Some extensions are blocked on the 18 upgrade
  path. Run Azure's upgrade validation checks.
- Application job definitions stored inside `azure_sys` (notably `pg_cron`)
  need a separate approved export/recreation procedure, not a restore of that
  managed database. When `azure_sys` is connectable, preflight checks `cron.job`
  and refuses a nonempty job catalog. If it is inaccessible, inventory jobs
  with your Azure administrator; their absence cannot be verified by this tool.
- Materialized views are refreshed by `pg_restore`, not restored as arbitrary
  frozen heap contents. Time-dependent/nondeterministic definitions can therefore
  fail the exact-data comparison. Volatile custom type output or changes in
  PostgreSQL's textual representation can also cause conservative mismatches.
- Complex non-owner ACL grant chains may need privileges Azure does not expose
  (`SET SESSION AUTHORIZATION`). Such a restore fails explicitly; rehearse with
  your actual roles, extensions and schema, not just a small sample.
- This is a **logical equivalence check**, not physical page/index corruption
  detection or proof of application correctness. Function/procedure definitions
  are compared, not executed. Keep Azure PITR as an independent recovery path.

## Prerequisites

When using Docker/AKS, the image supplies the software listed below; only
network access, credentials, storage and an appropriate runtime are external.

- Python **3.11+**, POSIX `sort`, and PostgreSQL client tools. Logical operations
  need no Python packages or Azure login. The separate PITR workflow needs Azure
  CLI authentication and Azure resource permissions.
- PostgreSQL **16** clients for a version-16 backup and version-16 restore.
  PostgreSQL **18** clients for a version-18 restore.
- PostgreSQL **18 `pg_dump` for schema comparison on every run**, including the
  initial 16 baseline. Keep the same 18 client build for subsequent comparisons.
  The separate 16 archive preserves the supported 16 -> 16 rollback path;
  an archive taken from 18 is never restored to 16.
- Direct primary-server connectivity, normally port 5432, not PgBouncer.
- A server administrator with CREATEDB/CREATEROLE, full read access including
  sequences and large objects, and permission to SET ROLE to application
  owners/grantors. RLS is explicitly disabled for scans: insufficient permission
  produces an error instead of validating a filtered subset of rows.
- A securely stored libpq password file (`PGPASSFILE`, mode 0600) with an entry
  for **each database**, or `*` in the database field. Provision the replacement
  server with the **same administrator name**.
- TLS defaults to `verify-full`. Configure `PGSSLROOTCERT` with the appropriate
  trusted CA bundle. Do not disable verification for Azure. `PGSSLMODE=disable`
  is used only by the isolated local integration tests.
- Sufficient encrypted local storage for archives and temporary sorting.
  Each table's temporary row digests need approximately **130 bytes per row**
  across input/output sort files, plus sort workspace. Configure `TMPDIR` on a
  suitably sized encrypted disk. Scans transfer all data to the client.

Backups take a full integrity scan before and after the dump. This detects
observed changes but is **not a cluster-wide transaction snapshot**. Only a
fully stopped workload provides the required cross-database consistency.

## Build and use the image

Run these commands from this directory on a machine with Docker:

```bash
make docker-build
make docker-test

# Optional full recovery rehearsal using the BUILT image and synthetic data.
# Also needs postgres:16 and postgres:18 images on the Docker host.
make test-image-integration

# Interactive shell, with /data persisted in a local named Docker volume:
make docker-run
```

The image defaults to `sleep infinity`: no operation starts automatically.
In AKS, `kubectl exec` opens a shell in the existing idle container. Locally,
`make docker-run` opens a shell directly. The image runs as UID/GID **10001**;
its default working directory is `/opt/postgresql-toolbox`.

The local named volume defaults to `azure-postgresql-backups`; select a distinct
`DATA_VOLUME` for each environment. **Do not remove the volume to clean up a
container.** A named volume survives the `--rm` container.

For real database access from a laptop, mount an existing protected password
file read-only. The entrypoint copies it to a private 0600 file because libpq
rejects group-readable projected Kubernetes Secret files:

```bash
docker run --rm -it --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --mount type=volume,source=azure-postgresql-backups,target=/data \
  --mount type=bind,source=/absolute/secure/path/pgpass,target=/credentials/pgpass,readonly \
  --env PGPASS_SOURCE=/credentials/pgpass \
  azure-postgresql-toolbox:local bash
```

The file must have entries for the source and recovery hosts and all application
databases (use `*` in the database field). Never put passwords in `ARGS`, a
Dockerfile, an image build argument, a committed YAML file, or shell history.

Use `/data` for backups **and reports**. Sorting uses `/data/tmp` on the same
volume; `/tmp` is ephemeral and reserved for private credentials/small temporary
files. Protect and size the backing disk appropriately. For an existing bind
mount rather than a named volume, ensure UID/GID 10001 can write to it.

The image uses the installed OS CA bundle with `sslmode=verify-full`. For an
enterprise/custom CA, mount an approved PEM bundle read-only and set
`PGSSLROOTCERT` to its path. No insecure TLS override is needed.

## Publish and run on AKS

### GitHub Actions and GHCR

The [container workflow](.github/workflows/container.yml) builds on pull requests,
pushes to `main`, and manual dispatch. Native GitHub-hosted AMD64 and ARM64
runners build each architecture and run `make docker-test test-image-integration`
against the packaged image. This includes synthetic PostgreSQL 16-to-16 and
16-to-18 recovery; it does not access Azure or production databases.

On `main`, each tested image is pushed to
`ghcr.io/<lowercase-owner>/<lowercase-repository>:sha-<full-commit>-<architecture>`.
Only after both architectures succeed does the publish job assemble and verify
the combined `sha-<full-commit>` image, then promote it to `main` and `latest`.
Pull requests never publish. Actions are pinned by commit SHA; build caches are
separate per architecture. Builds use Docker's build action for its native
Buildx/cache integration; validation and publishing operations use Make targets.

Authentication uses the workflow's short-lived `GITHUB_TOKEN` with
`contents: read` and `packages: write`. No PAT or Azure credentials are needed.
Public actions and the Debian, PostgreSQL and Microsoft package repositories
do not need a personal GitHub token. The token is not passed into the Docker
build or stored in the image. Organisation policy can still restrict Actions
or package publishing.

**A public GitHub repository does not automatically make its GHCR package
public.** After first publication, an administrator must check the package's
settings and change its visibility to **Public** if anonymous pulls are wanted.
Otherwise AKS needs an approved `imagePullSecret` with GHCR read access.
The image source label links the package to this repository.

For this repository:

```bash
docker pull ghcr.io/aksharagyana/az-postgresql:latest
docker buildx imagetools inspect ghcr.io/aksharagyana/az-postgresql:latest
```

The Actions run summary includes the multi-platform manifest digest. Record
that digest if you need to reuse an exact release later. CI publishes images only; it does not deploy to
AKS, authenticate to Azure, or run a real managed PITR operation.

### Manual publishing and AKS

**No image is pushed and no cluster resources are changed by `docker-build`.**
Publish to an approved registry accessible to AKS:

```bash
# Authenticate to your registry using your approved process first.
make docker-publish IMAGE=myregistry.azurecr.io/azure-postgresql-toolbox:migration-1
```

This uses Buildx to publish **linux/amd64 and linux/arm64**. Use
`PLATFORMS=linux/amd64` if you need only typical x86 AKS nodes. `docker-build`
builds/loads only the local architecture unless overridden with, for example,
`make docker-build PLATFORM=linux/amd64 IMAGE=azure-postgresql-toolbox:amd64`.
For repeatable recovery, an immutable image digest is available as an override.
**Use the exact same image for backup, restore and validation**.
Package repositories update over time; rebuilding the same tag is not a
reproducible client-version pin.

The [AKS manifests](k8s/) define a dedicated namespace, a 100-GiB Azure Disk PVC,
and a single-replica idle Deployment. It has no Service/Ingress, no mounted
Kubernetes API token, a read-only root filesystem, dropped capabilities, and
non-root execution. `Recreate` avoids two rollout replicas contending for the
ReadWriteOnce disk. The readiness check verifies local storage/credential access,
not database connectivity; no liveness probe kills a long-running operation.

The temporary interactive Deployment uses the public multi-architecture image:

```text
ghcr.io/aksharagyana/az-postgresql:latest
```

`make aks-render` and `make aks-deploy` default to the image in
`k8s/deployment.yaml`, not the local Docker build tag. An explicit `IMAGE=...`
still overrides it. `imagePullPolicy: Always` resolves the tag on each container
start; CI publishing `latest` does not restart an already running container.
Keep the same container throughout backup, restore and validation. A restart
can pick up a newer release, so record the actual image digest with your backup
records and use `IMAGE=ghcr.io/aksharagyana/az-postgresql@sha256:...` if you need
that exact release for later recovery.

Before deploying:

1. Confirm the AKS cluster/context and approved namespace. Restrict permission to
   exec into this pod and read its Secret/PVC; this pod has database admin access.
2. Ensure AKS can reach `ghcr.io` and its image download endpoints. The public
   package supports anonymous pulls, so no GHCR pull Secret is required.
   If overriding with a private registry, configure its pull permissions.
   Logical operations need no Azure API credentials;
   the PITR workflow requires a separate authorized Azure CLI login.
3. Ensure node/pod networking and private DNS can reach both source and replacement
   PostgreSQL FQDNs on TCP 5432. Allow appropriate DNS egress and registry/storage
   traffic in your organization's policies. An image does **not** bypass firewalls
   or create a private endpoint. No permissive NetworkPolicy is added here.
4. Review the storage class, capacity and resource requests/limits in the
   manifests. `managed-csi-premium` must exist and support your AKS configuration.
   The example's 100 GiB is a placeholder for sizing, not a production estimate;
   backup archives and scratch files share the disk. Ensure encryption and your
   retention requirements are met.
5. Provision a Secret named `postgres-toolbox-pgpass`, key `pgpass`, using your
   approved secret manager/Key Vault integration, or from an existing secure file
   as shown below. No example secret with passwords is committed.

```bash
# Check context before any mutation.
kubectl config current-context

# This explicit example uses the default dedicated namespace.
kubectl create namespace postgres-migration --dry-run=client -o yaml | kubectl apply -f -
kubectl -n postgres-migration create secret generic postgres-toolbox-pgpass \
  --from-file=pgpass=/absolute/secure/path/pgpass

# Review generated manifests before applying; uses GHCR latest with Always pull.
make aks-render
make aks-deploy
make aks-shell
```

All AKS Make targets accept `NAMESPACE=your-approved-namespace`. Create the
credential Secret in that same namespace. The non-root pod reads the projected
Secret via `fsGroup: 10001`, then copies it to `/tmp/pgpass` with mode 0600.
Credential copies refresh **on container startup**, not automatically when a
Secret changes. Only restart the Deployment to refresh credentials when no
backup/restore/validation is running. Do not mount a Docker socket or use
privileged mode.

Inside the shell, `pwd` is `/opt/postgresql-toolbox`, so the Make commands below
work unchanged. Alternatively, execute a single command without an interactive
TTY:

```bash
kubectl -n postgres-migration exec deployment/postgres-toolbox -- make test
kubectl -n postgres-migration exec deployment/postgres-toolbox -- \
  make backup PG_MAJOR=16 \
  ARGS='--host source.postgres.database.azure.com --user migration_admin --backup-dir /data/pre-upgrade-16 --quiesced'
```

Keep network access available until the command exits and check its exit status.
**An exec session is not a durable job.** Node drains, OOMs, pod restarts,
upgrades/rollouts and session failures can interrupt operations; a Deployment
recreates the idle toolbox, not the interrupted command. Plan a quiet maintenance
window and do not roll/restart the pod mid-operation. For unattended operations,
use a separately reviewed Kubernetes Job rather than relying on exec.

The PVC survives pod/container recreation. It does **not** protect against PVC,
namespace, cluster or underlying-disk deletion. Never delete the namespace/PVC
while a backup is needed; do not use `kubectl delete -k k8s` for routine cleanup.
Copy completed backups to independently protected storage before upgrading.
`kubectl cp` (supported by the image's `tar`) can retrieve a small backup:

```bash
kubectl -n postgres-migration get pods -l app=postgres-toolbox
# Replace POD_NAME with the actual name; destination should be protected/encrypted.
kubectl -n postgres-migration cp POD_NAME:/data/pre-upgrade-16 /secure/backups/pre-upgrade-16
```

For independent storage, use the optional Entra-authenticated Blob upload and
download below. Azure CLI is included; storage credentials are not.

## Backup, restore and validate commands

Run **inside the container/pod**. The backup directory must not already exist. Its parent
must exist. Report paths must also be new; errors exit nonzero.

`PG_MAJOR=16` is the image default. Always specify it explicitly for clarity.
The Makefile selects `/usr/lib/postgresql/16/bin` or `/usr/lib/postgresql/18/bin`;
schema comparison always uses the installed 18 dumper.
No credentials are placed on command lines.

```bash
# Test the tooling without connecting to any database:
make test

# 1. Stop every writer. Back up the entire source 16 server.
make backup PG_MAJOR=16 ARGS='--host source.postgres.database.azure.com --user migration_admin --backup-dir /data/pre-upgrade-16 --quiesced'

# 2. Rehearse on NEW, empty 16 and 18 Azure servers before production upgrade.
# This restores every included database, then automatically validates everything.
make restore PG_MAJOR=18 ARGS='--host recovery18.postgres.database.azure.com --confirm-target recovery18.postgres.database.azure.com --user migration_admin --backup-dir /data/pre-upgrade-16 --report /data/recovery18-report.json --quiesced'

# Recovery to a NEW version-16 server:
make restore PG_MAJOR=16 ARGS='--host recovery16.postgres.database.azure.com --confirm-target recovery16.postgres.database.azure.com --user migration_admin --backup-dir /data/pre-upgrade-16 --report /data/recovery16-report.json --quiesced'

# 3. Upgrade the ORIGINAL server through the portal; keep traffic stopped.
# Validate the upgraded server against its pre-upgrade backup:
make validate PG_MAJOR=18 ARGS='--host source.postgres.database.azure.com --user migration_admin --backup-dir /data/pre-upgrade-16 --report /data/post-upgrade-report.json --quiesced'
```

Host-native execution remains supported: install the prerequisites, leave
`PG_MAJOR` unset and supply `--bin-dir`/`--schema-bin-dir` in `ARGS`, or set
`PG_BIN_DIR` and `SCHEMA_BIN_DIR` to the appropriate local client paths.

`restore` refuses the recorded source endpoint and any target with additional
application databases or any application objects in `postgres`. Use a new Azure
server name, not a DNS alias of the source. It never uses `--clean`, drops a
database, or replaces an existing application role. Matching pre-provisioned
roles may be retained, but all their non-password attributes must match.

Restore is not atomic across the whole server. On any failure, keep traffic
stopped: the target can be partially populated. Inspect the error and use a
new/reprovisioned target for a retry; there is no destructive retry switch.

### Optional compressed data backup in Blob Storage

Unlike the PITR reference JSON, this uploads **actual logical database contents**:
all the application database dumps, roles and integrity baseline in one
`backup.tar.gz`. Azure-managed defaults remain excluded exactly as for local
backup. Only the manifest-listed backup files are bundled, not unrelated files
placed in the directory. Custom-format PostgreSQL dumps are already compressed;
the outer gzip primarily bundles them and compresses the SQL/JSON metadata.

The storage account and private Blob container must already exist. Authenticate
**inside the same toolbox container/pod** using Entra device-code login:

```bash
make azure-login ARGS='--tenant YOUR_TENANT_UUID'

# Stop all writers, then back up locally and upload the verified compressed copy.
make backup PG_MAJOR=16 ARGS='--host source.postgres.database.azure.com --user migration_admin --backup-dir /data/pre-upgrade-16 --quiesced --storage-account migrationstore --blob-container backups'
```

Both storage options must be supplied together. The command prints a URL such as:

```text
https://migrationstore.blob.core.windows.net/backups/data-20261117124525/backup.tar.gz
```

Prefixes use **UTC**, with clearly separate names even in the same container:

| Prefix | Contents |
| --- | --- |
| `data-YYYYMMDDHHMMSS/backup.tar.gz` | Portable logical backup with actual application data |
| `pitr-YYYYMMDDHHMMSS/preupgrade-pitr.json` | Reference to Azure-managed backups, not their data |

Omitting the storage options preserves the local-only workflow. Upload never
overwrites a Blob. On failure the command exits nonzero and the completed local
backup remains; the temporary compressed copy is removed. Do not retake a live
database backup just to retry uploading an existing one.

To download, verify, decompress and restore into a **new, empty** server:

```bash
make restore PG_MAJOR=18 ARGS='--host recovery18.postgres.database.azure.com --confirm-target recovery18.postgres.database.azure.com --user migration_admin --backup-url https://migrationstore.blob.core.windows.net/backups/data-20261117124525/backup.tar.gz --backup-dir /data/downloaded-16 --report /data/recovery18-report.json --quiesced'
```

Use `PG_MAJOR=16` and a new version-16 target for recovery to 16. `--blob-url`
is an alias for `--backup-url`. The extraction directory and report must be
**new**, with existing writable parents. After download, the extracted backup
remains available for `make validate` or another restore using `--backup-dir`
**without** the URL option.

Download requires this project's archive format and its `sha256` Blob metadata.
The compressed SHA-256, ETag/size and all internal manifest/file checksums are
verified **before any database connection**. Unsafe tar paths, links, devices,
directories, duplicate entries and files outside the backup inventory are
rejected. Files are extracted as 0600 under a 0700 directory without replaying
archive ownership or permissions. Invalid extraction can leave an incomplete
local directory; inspect it and choose a new path rather than overwriting it.
Restore still runs the full data-integrity validation automatically.

All transfers use `--auth-mode login`; no shared keys, connection strings or SAS
URLs are accepted as authentication. The login needs **Storage Blob Data
Contributor** to upload and **Storage Blob Data Reader** (or Contributor) to
download, in addition to the normal PostgreSQL access. Storage firewall/private
endpoint/DNS rules must allow HTTPS from the toolbox. Only standard HTTPS Azure
public, US Government and China Blob URLs without query strings are supported.
See [PITR authentication details](PITR.md#optional-upload-the-recovery-reference-to-blob-storage).

Allow disk space for **the local backup plus its compressed archive on upload**,
and **the downloaded archive plus its expanded files on restore**, in addition
to normal backup/validation scratch space. Temporary archives use the backup
directory's parent on the same volume and are removed after the transfer.
Unlike PITR JSON, data archives have no 1-MiB limit; downloads/extraction check
available disk space. Independently protect the Blob container and retention:
uploading a backup does not configure immutability or disaster-recovery policy.
Blob command-contract tests are mocked; rehearse a live transfer before production.

## Integrity validation and reports

`validate_server` compares the live server with the saved pre-upgrade baseline:

1. Exact application database and custom role/membership inventories.
2. Schema-only SQL generated using the same PostgreSQL 18 dumper, including
   routine bodies, constraints, indexes, policies, privileges and comments.
   Only dump version headers and random psql restriction keys are normalized.
3. Every physical user table/partition/inheritance child and populated
   materialized view: **all rows, not a sample**. SHA-256 row digests are sorted
   externally, then hashed again, so row order is irrelevant and duplicate
   multiplicity is preserved. `ONLY` prevents counting child rows twice.
4. Sequence `last_value` and `is_called`, including unused sequences.
5. Every large object, read in 64-KiB chunks, plus its OID/owner/ACL.
6. Database properties/settings, public-schema ACLs and extension versions.

A completed scan writes a JSON report containing `passed`, `differences` and
the actual fingerprints. Changed table values are detected even when row counts
match. Missing/extra objects and any schema/extension-version differences fail.
Preflight/connection/permission failures exit nonzero and may occur before a
report can be written. Review legitimate upgrade differences explicitly; do
not edit the baseline or ignore a failed report.

After a pass, reset custom login passwords as needed, verify Entra/Azure
configuration, run ANALYZE with appropriate privileges, run application smoke
tests, and only then reopen traffic. If production is managed by Terraform,
update its requested major version to 18 before the next apply.

## Backup handling

The CLI uses umask 077; the backup directory is 0700 and JSON reports are 0600.
Archives and role settings can contain sensitive data or connection strings:
keep them **outside the repository**, on encrypted storage, with restricted
access. Copy the completed directory to independent protected storage and verify
its checksums there before upgrading. An absent/invalid manifest checksum means
the backup is incomplete or damaged.

Checksums detect accidental corruption, not an attacker who can rewrite the
manifest and checksums. Restore only trusted backups: PostgreSQL restores execute
source-defined SQL. Do not publish dumps or reports.

## Reuse and tests

Import the module and use `Server(...)`, `backup_server(server, directory)`,
`restore_server(server, directory, report, confirm_target)`, and
`validate_server(server, directory, report)`. Wrap calls in `private_files()`
when embedding, and stop writers before invoking them. `BackupError` signals
an unsuccessful operation; never continue cutover after it.

`backup_server` also accepts `storage_account=` and `blob_container=`.
`restore_server` accepts `backup_url=` with a new extraction directory.
`upload_backup(directory, account, container)` uploads an existing validated
backup and returns its URL, allowing transfer retries without a new database
backup. `download_backup(url, directory)` verifies and extracts without touching
a database. Both reuse the same Entra-only Blob helpers as PITR.

```bash
make test
make test-integration
make test-image-integration
```

The opt-in integration test needs Docker and the `postgres:16` and `postgres:18`
images (pull these if missing). It creates only uniquely named test containers,
an internal network with no published ports, and temporary synthetic data.
It cleans up its own containers, volumes, network and temporary files.

`make test` works **inside AKS without Docker**, using only synthetic local unit
tests; it does not validate production data. Use `make validate` for that.
`test-integration` runs host-driven database rehearsals; `test-image-integration`
executes actual Make backup/restore/validate commands in the built, non-root,
read-only toolbox image and checks volume persistence across a restart. Both
integration targets run **on a Docker host outside AKS**, not inside the pod.

It rehearses 16 -> 16 and 16 -> 18 using a non-superuser migration administrator,
simulated protected Azure roles/databases/public-schema ownership, custom grant
chains, empty databases, unusual names, duplicate/null/binary/JSON data,
partitions, inheritance, routines, triggers, domains, sequences, materialized
views, ACLs and a multi-chunk large object. It tests changed values, routines,
and sequences, existing-target refusal, and unsupported foreign tables.
**This is not certification against the actual Azure service.** Rehearse both
recovery paths on your actual Azure schema before the maintenance window.

## Authoritative references

Reviewed 2026-11-16:

- [Microsoft: dump and restore; Azure role/password restrictions](https://learn.microsoft.com/en-us/azure/postgresql/migrate/how-to-migrate-using-dump-and-restore)
- [Microsoft: major version upgrade and rollback/PITR behavior](https://learn.microsoft.com/en-us/azure/postgresql/configure-maintain/concepts-major-version-upgrade)
- [Microsoft: restricted roles and Azure public-schema ownership](https://learn.microsoft.com/en-us/azure/postgresql/security/security-access-control)
- [PostgreSQL: pg_dump scope, compatibility, extensions and restore trust](https://www.postgresql.org/docs/18/app-pgdump.html)
- [PostgreSQL: official Debian packages, signing key and supported architectures](https://www.postgresql.org/download/linux/debian/)
- [Kubernetes: non-root security contexts and volume fsGroup](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/)

Azure can use its implicit pre-upgrade backup to recover from an upgrade error.
After a **successful** major upgrade, there is no in-place downgrade: use PITR
to a pre-upgrade point on a new server, or the rehearsed version-16 dump restore.
The logical `server_backup.py` script does not invoke PITR or Azure's internal
failure recovery. Use [the separate PITR workflow](PITR.md) for managed backup
and new-server restore.
