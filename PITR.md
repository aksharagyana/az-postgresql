# Azure-managed backup and point-in-time restore

`azure_pitr.py` contains two reusable functions:

1. `take_pitr_backup(...)`: create an Azure on-demand backup of the entire
   Flexible Server and save its verified recovery reference.
2. `restore_pitr_backup(...)`: use that reference to ask Azure to **create a new
   server** at the saved point in time.

No upgrade is performed. Upgrade manually in the portal **only after backup
finishes successfully**. If the upgrade works, no restore is needed.

## What "backup" means here

Azure already maintains physical backups and transaction logs. This script
triggers an additional named on-demand backup using the PostgreSQL management
API through Azure CLI. It waits for the operation and checks the named backup's
`completedTime` and the server's reported recovery window.

The local JSON contains the source/backup resource IDs, tenant/subscription,
source major version, exact recovery timestamp and reference metadata.
**It is NOT a copy of the database or a portable backup.** Keep a copy of this
reference outside the source server/pod, but understand that recovery still
depends on Azure retaining the actual backup.

The function backs up the whole server through Azure; it does not enumerate
databases, copy system objects, alter roles, or run SQL against Azure defaults.
Restore likewise lets Azure handle its own service-managed database state.

## Requirements

- Python 3.11+ and Azure CLI; both are included in the **rebuilt** toolbox image.
  Existing published images must be rebuilt/pushed/redeployed to include this
  script. Azure CLI **2.93.0** command flags were checked during development.
- Authenticate before running. For interactive use, run
  `az login --use-device-code` using an approved account. For AKS automation,
  configure an approved managed/workload identity integration and Azure CLI
  login explicitly; the script never selects a node identity automatically.
  The existing AKS manifests do not configure workload identity.
- The identity needs management-plane permissions to read the source server and
  its backups, create an on-demand backup, list servers in the target resource
  group, and create the target restored server. Private network integration
  also requires the appropriate subnet/private-DNS permissions. Have your Azure
  administrator approve least-privilege RBAC.
- Azure Resource Manager and authentication endpoints must be reachable.
  **No database password or TCP-5432 access is needed for these two functions**.
  Data validation and application tests still need database network access.
  The original AKS deployment's password Secret is for the logical tools, not
  consumed by PITR.
- The source must be `Ready` for backup. Its control-plane resource and named
  backup must still be accessible for normal PITR restore; the source database
  engine need not be operational or `Ready` when restoring.
- Choose an existing target resource group in the **same subscription**.
  This script restores in the **same region and network configuration** as
  the source. It supports the 16/18 workflow, not geo-restore or deleted-server
  recovery. Customer-managed encryption keys and elastic clusters need a
  separately reviewed restore workflow.
- Stop **all writers**, background jobs and DDL before backup, and leave them
  stopped through upgrade/rollback and verification. Azure can back up an active
  server, but quiescence is required by this migration procedure to avoid losing
  post-backup changes. `--quiesced` acknowledges that operational responsibility;
  it does not stop applications.

In the toolbox, `AZURE_CONFIG_DIR=/tmp/.azure` holds ephemeral CLI authentication.
Do not bake credentials into the image or commit them. A pod restart removes
this login state; reauthenticate when needed. The backup reference should be
saved under persistent `/data`, **not** `/tmp`.

## 1. Take a managed backup before the portal upgrade

Run inside the toolbox shell, or from this directory on a machine with Azure CLI:

```bash
make pitr-backup ARGS='--subscription 11111111-1111-1111-1111-111111111111 --resource-group production-rg --server production-pg --checkpoint /data/preupgrade-pitr.json --quiesced'
```

Replace the example subscription UUID, resource group and server. An Azure
backup name is generated automatically; optionally add
`--backup-name unique-preupgrade-backup-name`.

The checkpoint path must be **new**, with an existing writable parent directory.
After success, it contains `status: "backup_ready"` and a `restore_time` returned
by Azure, not the laptop's guessed current time. Duplicate backup names, incomplete
backup responses and changes in source version/state/networking are errors.

**Only after success:** copy the JSON to protected independent storage, confirm
the backup in the Azure portal, and perform the manual upgrade. Run the normal
post-upgrade data/application tests. Keeping the JSON does not extend backup
retention.

## 2. Restore only if rollback is required

Do **not** pre-create an empty database server. Supply a globally unique,
different server name; Azure creates it as part of PITR:

```bash
make pitr-restore ARGS='--checkpoint /data/preupgrade-pitr.json --target-resource-group recovery-rg --target-server production-pg-recovery16 --confirm-target production-pg-recovery16 --report /data/pitr-restore-result.json'
```

The target RG must already exist. The explicit `--confirm-target` acknowledges
creation of a **billable new Azure server**.

Restore:

- Reads the saved tenant/subscription and full source resource ID, avoiding
  accidental use of the CLI's current default subscription.
- Rechecks that the recovery point remains in the current retention window and
  that the named backup still has the recorded completion time.
- Refuses a source whose creation metadata/network/location changed, a
  mismatched login tenant, or a target server that already exists.
- Always supplies the **saved pre-upgrade timestamp**. A source now running 18
  is allowed; if the backup was taken on 16, the restored server must report 16.
  The script never substitutes "now"/"latest", requests a cross-major restore,
  upgrades a server, deletes anything, or overwrites an existing server.
- Waits for Azure's restore operation, then verifies the target resource ID,
  `Ready` state and expected major version. A successful report has
  `status: "server_ready"` and the new FQDN.

**`server_ready` is not a data-integrity or application-test result.**
The report explicitly sets `data_integrity_validated: false`. Before cutover,
verify all databases/data, server parameters, authentication, firewall/private
endpoints/DNS, HA/replicas and application behavior. Update connection strings
only after these checks. If a separate logical backup baseline was captured,
`make validate` can perform its full logical-content comparison.

Backup/restore commands can take a long time. Keep the exec session connected;
do not restart the pod or roll the Deployment while an operation is running.
Errors return nonzero. An interrupted or failed operation can still be running
in Azure, or can have created a target server. Inspect Azure before retrying.
Pending JSON files retain `backup_requested`/`restore_requested` rather than
claiming success; completed files are atomically replaced and not overwritten
by a new invocation.

## Reusable Python functions

```python
from pathlib import Path
from azure_pitr import take_pitr_backup, restore_pitr_backup

checkpoint = Path("/data/preupgrade-pitr.json")
take_pitr_backup(
    "11111111-1111-1111-1111-111111111111",
    "production-rg",
    "production-pg",
    checkpoint,
    quiesced=True,
)

# Later, ONLY if recovery is needed:
restore_pitr_backup(
    checkpoint,
    "recovery-rg",
    "production-pg-recovery16",
    Path("/data/pitr-restore-result.json"),
    confirm_target="production-pg-recovery16",
)
```

`PitrError` indicates an unsuccessful operation. Output JSON is created with
mode 0600. Neither function logs in, changes the account default, changes backup
retention, or deletes source/target resources.

## Limits and verification

Built-in backups are in **Microsoft-managed storage**, not your PVC or your
storage account. The PVC holds only the recovery reference/report. Retention
is controlled by Azure/server settings (normally 7-35 days). On-demand backups
are not an independent backup-vault archive.

Do not delete the source server as a recovery step. Deleted-server recovery
has a separate short recovery window and is not guaranteed. If the required
backup is gone or a region is unavailable, this normal PITR script is not a
fallback for an independent backup or a geo-recovery strategy.

`make test` includes mocked Azure command-contract and safety tests. Those
tests never call an Azure subscription. Container checks verify the installed
CLI and command availability; **they do not prove a live Azure restore**.
Rehearse both functions against a non-production Azure server before relying
on this for production.

### Microsoft references

Verified 2026-11-16:

- [On-demand backups](https://learn.microsoft.com/en-us/azure/postgresql/backup-restore/how-to-perform-backups)
- [Exact Azure CLI backup flags](https://learn.microsoft.com/en-us/cli/azure/postgres/flexible-server/backup?view=azure-cli-latest)
- [Restore to a custom point](https://learn.microsoft.com/en-us/azure/postgresql/backup-restore/how-to-restore-custom-restore-point)
- [Backup completion timestamp API](https://learn.microsoft.com/en-us/rest/api/postgresql/backups-automatic-and-on-demand/get?view=rest-postgresql-2024-08-01)
- [Upgrade rollback to an earlier version](https://learn.microsoft.com/en-us/azure/postgresql/configure-maintain/concepts-major-version-upgrade)
