# Compose recovery: encrypted backup and offline staging

`scripts/compose-recovery.py` is an operator-run tool for the `core-preview`
Compose topology. It does **not** switch a running installation to restored
data. The successful LXC rehearsal established the backup, restore and
rollback sequence; this repository tool currently makes its archive creation,
verification and offline extraction repeatable. A live cutover requires a
separate reviewed procedure.

The script never prints environment values, passwords, paths, Docker logs or
the contents of a failed command. Run it as root on the Docker host, not inside
an agent. Keep the backup directory and encryption-key directory on separate
storage, both pre-created and owner-only (`0700`). Do not copy either into Git, an image build
context, a chat, or a CI artifact. A valid backup requires **both** the archive
and its separate key; losing the key makes the backup unrecoverable.

## Backup

Use absolute paths. The project directory must contain the private `.env` and
`compose.yaml`; the state directory must be the bind-mounted NanoClaw data root.
The two output roots must not overlap each other, the project, or the state.

```sh
python3 scripts/compose-recovery.py backup \
  --project-root /path/to/checkout \
  --state-root /srv/nanoclaw \
  --backup-root /path/to/private-backups \
  --key-root /path/to/separate-private-keys
```

The command above is a preflight. It checks the release/upgrade marker, SQLite,
Compose, OneCLI/PostgreSQL volume mounts, private inputs and exact agent
identity without stopping services. To create the backup, rerun with `--apply`
after scheduling a maintenance window. The script stops NanoClaw and its
writers, dumps PostgreSQL while it is still running, then stops PostgreSQL and
archives NanoClaw state, `.env`, OneCLI `/app/data`, and the broker/password
files named by `.env`. It encrypts the archive and dump, removes the plaintext
intermediates, records checksums and an HMAC-authenticated manifest, and
attempts to restart only the services that were running before the backup,
even on failure. Services already stopped stay stopped. Plaintext archives
exist briefly in the private backup directory while encryption runs; use a
host with suitably protected storage. Status output is
redacted; inspect the private output roots locally to identify the new pair.

If `original_stack=restart_failed`, do not start another backup or delete any
artifact. Diagnose the original installation first. A `backup=verified` line
does not override a later restart failure.

## Verify and stage without cutover

Supply the backup directory and its matching key file explicitly. Verification
decrypts into a temporary owner-only directory, checks the HMAC, both hashes,
archive member safety and release identity, then removes the plaintext.

```sh
python3 scripts/compose-recovery.py verify \
  --backup-dir /path/to/private-backups/BACKUP_ID \
  --key-file /path/to/separate-private-keys/BACKUP_ID.key
```

`stage` requires a **nonexistent absolute** target directory below an existing
owner-only (`0700`) parent, and an explicit
confirmation because it leaves sensitive plaintext there. It extracts the
state, `.env`, OneCLI data, private inputs and PostgreSQL dump; it verifies the
central SQLite database. The target is `0700`. Its contents are not a running
installation and must not be exposed by a web server or included in a build.

```sh
python3 scripts/compose-recovery.py stage \
  --backup-dir /path/to/private-backups/BACKUP_ID \
  --key-file /path/to/separate-private-keys/BACKUP_ID.key \
  --target-dir /path/to/new-private-staging \
  --confirm-sensitive-plaintext
```

The archive excludes transient sockets and other special files: services
recreate their sockets on startup. It preserves only symbolic links below
`state/data/` whose absolute target is under the container's `/app` tree,
without following them on the host. Extraction writes regular files and
directories first, then recreates those validated links; all other links,
hard links and unsafe member paths are rejected. The extractor retains numeric
UID/GID but strips setuid/setgid/sticky bits and group/other write bits. Never
run two NanoClaw hosts against the same state or agent containers. A future live
restore must create new OneCLI/PostgreSQL volumes, restore the PostgreSQL dump,
check ownership and service health, and provide a rollback path before
switching the host. Neither `verify` nor `stage` performs those operations.

The staged copy preserves numeric UID/GID and file modes from the authenticated
archive. UID 1000 services need that ownership for their bind-mounted state and
broker files; changing ownership during extraction can make several services
restart even when PostgreSQL restores successfully.

These commands are not a substitute for testing backup restoration with
separate identities before migrating a real agent. Preserve the old machine
and its encrypted backup until the migrated instance has been checked.

## Cutover preflight (read-only)

After a successful `verify` and `stage`, `scripts/compose-cutover.py` checks
that the staged data still matches the active checkout/release and environment,
that its database and PostgreSQL dump are readable, that link types are safe,
and that the original agent identities and OneCLI/PostgreSQL volume mounts are
the expected ones. It does not create volumes, stop services, swap files or
send an agent prompt. It has no `--apply` option.

```sh
python3 scripts/compose-cutover.py \
  --project-root /path/to/checkout \
  --state-root /srv/nanoclaw \
  --stage-dir /path/to/private-staging
```

Only run this as root on an isolated test install. It is a prerequisite for a
future guarded rehearsal, not authorization to restore into a live install.
If the scripts arrive through a writable transfer directory such as `/tmp`,
copy both `compose-cutover.py` and its sibling `compose-recovery.py` into a
root-owned `0700` directory, verify their pinned checksums, and run the copies
there; never execute a mutable transfer copy as root.
The staged directory is plaintext and remains operator-owned; never place it
inside Git, a Docker build context or a web-served directory. A later
implementation must prove rollback before any production migration.

## Synthetic restore rehearsal (test-only)

`scripts/compose-rehearsal.py` is a separate, explicitly mutating test-only
transaction. Choose either `--preflight` or `--apply`, always with
`--confirm-synthetic`. Preflight authenticates and extracts a new copy into a
root-only work area but does **not** stop or replace the running stack; it
leaves that plaintext copy for inspection. Apply additionally performs the
runtime switch and rollback. The command refuses
non-CLI messaging groups and nonempty Telegram/Signal account settings, and
acquires an exclusive lock. It authenticates the encrypted backup and creates
a **fresh** staged copy under an operator-provided root-only work directory on
the same filesystem as the active state; no previous staging directory is
required. The fresh copy passes the read-only preflight before
the original is stopped. It then switches the state path, restores OneCLI and
PostgreSQL into new named volumes, checks health and a synthetic CLI `READY`
prompt, and always attempts to return to the original state and original
volumes, followed by another prompt. It never swaps the active `.env`: the
preflight requires its bytes to match the backup.

The command deliberately retains transaction data, recovered state and new
volumes for inspection; these contain sensitive plaintext. Rollback failure
prints `original_rollback=failed_manual_recovery_needed` and requires manual
recovery, not an automatic retry. This script is **not a production migration
or update command**. On the isolated Docker LXC, the latest rehearsal verified
an authenticated synthetic backup, staged it with numeric ownership and safe
modes, restored onto fresh volumes, and rolled back automatically with `READY`
prompts on both sides. The selective service restart after creating a backup
is covered by local tests; this rehearsal used an existing backup. Do not run
it with real channel identities, credentials or workloads. A root-only LXC
`--preflight` and separate review are required before its first `--apply` run;
no production use is authorized.

If restore fails after stopping the original, the script prints a fixed
`restore_failed_phase` label and only the Compose service state/health enums
available at that moment, then attempts rollback. It never prints command
stderr or container logs. Retain the failed transaction and its new volumes
until the phase has been diagnosed; `original_rollback=healthy` confirms the
original volumes were reattached and a synthetic prompt succeeded.
