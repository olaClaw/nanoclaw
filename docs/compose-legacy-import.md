# Importing a checkout-based install into Compose

Two operator-run tools move an existing checkout-based NanoClaw install (host
and mail broker as systemd user units, OneCLI and PostgreSQL in Docker) onto
the `core-preview` Compose stack. They require the same NanoClaw schema on
both sides; the snapshot records the applied migrations and the import refuses
any difference.

Both tools print fixed status labels only. Run each one as root from a
root-owned `0700` directory together with its sibling `compose-recovery.py`
(and `legacy-snapshot.py` for the import), after verifying their reviewed
SHA-256 hashes. Never run a user-writable transfer copy as root.

## 1. Snapshot the old install

```sh
python3 legacy-snapshot.py \
  --project-root /path/to/old/checkout --service-user SERVICE_USER \
  --output-root /root/private-snapshots --key-root /root/private-snapshot-keys
```

Without `--apply` this is a read-only preflight: clean checkout, central DB
integrity, active host unit, mail broker config, Signal state, one OneCLI and
one PostgreSQL container, free space. With `--apply` it stops the host unit
(which also stops a host-managed Signal daemon), agent containers, the mail
broker unit and OneCLI; dumps PostgreSQL while it is still running; archives
`data/`, `groups/`, `.env`, the Signal state, `~/.config/nanoclaw`, the mail
broker config and OneCLI `/app/data`; restarts everything; and only then
encrypts the archive and dump. The pause lasts as long as the archive takes.
Hard links inside the archived state (for example package installs in a
group workspace) are kept; if the snapshot fails, its partial folder and key
are removed.

If it prints `original_install=restart_failed_manual_check_needed`, check the
old install first. The key is written to the separate key directory: copy it
into a password manager, keep an offline copy, and remove it from the machine
once the snapshot has been verified. Move the snapshot directory to the
Compose host with its permissions intact.

## 2. Import on the Compose host

The Compose host needs a verified `compose-recovery.py backup` of its current
release (`--target-backup-dir`, `--target-backup-key`): recreating the OneCLI
and PostgreSQL volumes can only be undone through it. The work root must be a
root-only directory on the same filesystem as the state root.

```sh
python3 compose-import-legacy.py --mode rehearsal \
  --project-root /path/to/compose/checkout --state-root /srv/nanoclaw \
  --snapshot-dir /root/private-snapshots/SNAPSHOT_ID --key-file /root/keys/SNAPSHOT_ID.key \
  --work-root /root/import-work \
  --target-backup-dir /root/backups/BACKUP_ID --target-backup-key /root/backup-keys/BACKUP_ID.key
```

Without `--apply` the command authenticates and unpacks the snapshot, checks
the target (release, checkout, backup, service health, schema, mail config,
space) and removes the plaintext again. With `--apply` and
`--confirm-replace-target-state` it:

1. stops the Compose stack and sets the current `data/`, `groups/`, `store/`,
   Signal state, `.env` and mail config aside inside the work root;
2. installs the imported `data/` and `groups/` owned by UID 1000, keeping the
   target's release marker;
3. points the `infomaniak_mail_readonly` MCP server at
   `http://infomaniak-mail:18765/mcp` (header kept) and marks the old host's
   running containers as stopped;
4. writes the mail broker config with the same credentials and token, listening
   inside its container and downloading under `/srv/nanoclaw/groups/`;
5. carries model and timezone settings from the old `.env`;
6. recreates the OneCLI and PostgreSQL volumes from the snapshot (OneCLI
   upgrades its own schema on start);
7. starts the stack and compares the central DB counts with the snapshot.

`--mode rehearsal` pauses every pending task, closes pending chat messages,
installs **no** Signal account state and refuses Signal or Telegram
identities in the target `.env`, so the copy cannot reach real contacts. The
mail broker does use the real, read-only credentials. `--mode cutover` keeps
queues, tasks and Signal state and carries the Signal and Telegram
identities; run it only when the old install is stopped for good, because two
instances with the same identities steal each other's messages.

On failure the command prints `import_failed_phase=…`. The previous state
stays in the work root and the target backup is untouched; no automatic
rollback is attempted. A rehearsal can be rerun after fixing the cause. For a
cutover, the old install is the rollback: stop the Compose stack and start the
old units again.
