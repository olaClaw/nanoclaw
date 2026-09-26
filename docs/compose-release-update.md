# Synthetic Compose release update

`scripts/compose-release-update.py` turns the tested LXC release cutover into
a versioned, parameterized transaction. It is **synthetic-only**: it refuses
non-CLI messaging groups, configured Telegram or Signal identities, running
agent containers, a dirty checkout, a version change, changed external image
pins, unhealthy services and a missing or unverifiable encrypted backup. It is
not a production upgrade command or a data migration procedure.

The transaction changes the checkout, the three fork image references in
`.env`, the active release manifest, the upgrade marker, and the affected
Compose host/proxy/broker containers. OneCLI, PostgreSQL and Signal image pins
must remain unchanged. It does not replace state or database volumes. If a
step fails after stopping the host, it attempts to restore the previous
checkout and control files and recreate the previous services. A successful
rollback means those controls and services are healthy; it cannot undo a
database migration performed by a newer host. Keep the full encrypted backup
and its separate key until a restore has been rehearsed.

Run as root on an isolated test Docker host. Obtain the target manifest from
a reviewed successful private-image CI run, pin all images by digest, and
fetch its Git commit without switching the active checkout. Keep the manifest,
backup, key and control-backup root outside Git and all Docker build contexts.
The backup and key must already pass `compose-recovery.py verify`; the update
preflight verifies them again. Use distinct root-owned `0700` directories for
the encrypted backup, its key and control-file backups, ideally on separate
storage for the encrypted backup and key.

Copy the update script **and its sibling `compose-recovery.py`** from a trusted
checkout into a root-owned directory; verify their reviewed SHA-256 hashes
before executing. A user-writable transfer copy must not be run as root. The
target release's source tree and image labels are checked against the manifest.

```sh
python3 /private/operator-tools/compose-release-update.py \
  --project-root /path/to/checkout \
  --state-root /path/to/state \
  --release-manifest /path/to/private/release.json \
  --backup-dir /path/to/private-backups/BACKUP_ID \
  --key-file /path/to/separate-private-keys/BACKUP_ID.key \
  --control-backup-root /path/to/private-control-backups \
  --confirm-synthetic
```

Without `--apply`, the command is a preflight only. It prints fixed status
labels, not paths, secret values, Docker logs or command output. The manifest
and all three fork images must already exist locally; the command never logs
in to a registry or pulls images. If preflight succeeds, schedule one
maintenance attempt by adding `--apply` to the same command. Do not repeat a
failed apply automatically. If it prints
`rollback=failed_manual_recovery_needed`, keep the encrypted backup, key,
control backup and stopped containers intact for manual diagnosis. Even after
`release_update=healthy`, independently verify the checkout, release marker,
Compose service health, a synthetic CLI `READY` prompt and the agent image ID.

This tool intentionally does not offer a real-identity override. Before any
production use, design and rehearse a separate upgrade path that handles
state/schema changes, active agent sessions, intentionally stopped services,
private credential migration and full-state rollback.
