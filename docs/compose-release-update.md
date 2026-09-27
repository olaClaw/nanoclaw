# Compose release update

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

## Production mode

An install with real channel identities uses `--confirm-production` instead of
`--confirm-synthetic`. Everything above still applies (clean checkout,
digest-pinned images whose labels match the manifest, unchanged OneCLI,
PostgreSQL and Signal pins, control-file backup and automatic rollback), plus:

- **Same schema only.** The preflight compares, between the current and the
  target commit, every file under `src/db/migrations/`, the module
  `migrations/` directories and `src/mailbox/sqlite/schema.ts`. Any difference
  stops it with `schema_change_requires_full_restore`: the rollback restores
  code and control files, not data, so a release with new migrations needs a
  procedure that also restores the data. A version bump is allowed.
- **Fresh backup.** The encrypted backup must be of the current release, pass
  `verify`, and be at most `--max-backup-age-minutes` old (default 120).
- **Running agents.** They are allowed at preflight and stopped right after the
  host; their pending messages stay queued and are processed after the start.
- **After the start** the command waits for the expected channel adapters in the
  host log (CLI, plus Signal and Telegram when configured, and `Signal channel
  connected`), then waits until every per-group image is built on the new base
  (its `derived-from` and revision labels). The host usually starts that
  rebuild by itself; the command triggers one only when the host has not
  started it. Either check failing triggers the rollback.

Create the backup with `compose-recovery.py backup --apply` just before the
update, save its key in the password manager, and remove the key from the host
once the update has been checked. Plan a short maintenance window: the host is
stopped for the recreate and the per-group image rebuild.

## Job state

Every run, preflight-only or `--apply`, records its state in the control
backup root, so the terminal and the future dashboard read the same job:

- `compose-release-update.job.json` (root-owned, `0600`): the current or last
  run, rewritten atomically at every phase.
- `compose-release-update.jobs.jsonl`: the last 50 finished runs, without their
  phase timeline.

A record holds only fixed codes, commit IDs, the release version and UTC
timestamps: `job_id`, `mode`, `apply`, `from_revision`, `to_revision`,
`to_version`, `phase`, `outcome`, `failure_category`, `rollback`,
`started_utc`, `updated_utc`, `finished_utc` and `phases` (each phase with its
start time). No paths, identities, values or command output.

| Field | Values |
| --- | --- |
| `phase` | `preflight`, `control_backup`, `stop_host`, `switch_release`, `start_services`, `wait_channels`, `refresh_images`, `rollback` |
| `outcome` | `running`, `preflight_ok`, `succeeded`, `failed` (nothing changed), `rolled_back`, `rollback_failed`, `interrupted` |
| `rollback` | empty, `not_needed`, `healthy`, `failed_manual_recovery_needed` |

Read it without changing anything:

```sh
python3 /private/operator-tools/compose-release-update.py \
  --control-backup-root /path/to/private-control-backups --status [--json]
```

The run prints `job_id=...` at the start and `job_outcome=...` at the end.
`--status` reports `interrupted` for a `running` record when no run holds the
update lock, i.e. the process died (killed, host reboot). A run that died after
the preflight may have left the release half switched, so **every** later run
stops with `previous_update_interrupted`. Check the checkout, control files and
services by hand, restore from the backup if needed, then pass
`--acknowledge-interrupted JOB_ID` with the ID from `--status` to run again. A
second run started while one holds the lock stops with
`update_already_running`. Failing to write the state prints
`job_state=write_failed` once and does not stop the update or its rollback.
