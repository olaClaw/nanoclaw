#!/usr/bin/env python3
"""Portable export and import between Compose installs (D5).

Operator-run as root, or by the operations service. Only fixed status labels
are printed.

export  From a verified `compose-recovery.py` backup, write one portable file
        `<export-root>/<id>.ncx`: an uncompressed tar holding `manifest.json`,
        `state.tar.enc` and `postgres.dump.enc`, encrypted like the backups
        (AES-256-CBC with PBKDF2, SHA-256 of both sides, HMAC-authenticated
        manifest) under a fresh random key written to `<key-root>/<id>.key`.
        The key is never inside the file. Left out, because they belong to
        the machine: the proxy (certificates, DNS token), the dashboard's
        administrator state, the panel's model settings and the dashboard
        public-ID key.

import  Authenticate a portable file with its key in a root-only work area,
        check the release (the target must run the same release or a newer
        one, which migrates the data at start), the schema and the target's
        own verified backup, then, with `--apply`, replace the target's state:
          rehearsal  a test copy: no Signal account state, no Signal or
                     Telegram identity in `.env`, pending tasks paused and
                     pending chat closed, so it never talks to real contacts;
          migration  the real move: queues, tasks, Signal state and channel
                     identities are kept. Only after the source is stopped.
        A target that already has agents or chats is only replaced with
        `--confirm-replace-target-state`. Machine-specific settings (images,
        install ID, LLM endpoint, proxy, dashboard, paths) stay the target's.
        A failed apply leaves a transaction to undo with `--rollback-txn`.
"""

import argparse
import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECOVERY = _load('compose_recovery', 'compose-recovery.py')
LEGACY = _load('compose_import_legacy', 'compose-import-legacy.py')

SCHEMA = 'nanoclaw-compose-export/v1'
PARTS = ('manifest.json', 'state.tar.enc', 'postgres.dump.enc')
# Machine-owned state that never travels (the target keeps its own).
EXCLUDED = ('state/proxy', 'state/dashboard', 'state/data/model-settings.json', 'state/data/dashboard')
PRESERVED_DATA = ('model-settings.json', 'dashboard')
# NanoClaw's own state below the state root, replaced by an import.
STATE_ENTRIES = ('data', 'groups', 'store', 'signal', 'signal-outbox')
# `.env` keys an import carries; everything else stays the target's.
CARRY_KEYS = ('ASSISTANT_NAME', 'TZ', 'DEFAULT_AGENT_PROVIDER', 'NANOCLAW_DEFAULT_MODEL', 'OPENCODE_PROVIDER',
              'OPENCODE_MODEL', 'OPENCODE_SMALL_MODEL', 'OPENCODE_AUTH_MODE', 'OPENCODE_MODEL_CONTEXT_LIMIT',
              'OPENCODE_MODEL_OUTPUT_LIMIT', 'OPENCODE_MODEL_INPUT_MODALITIES', 'ONECLI_API_KEY',
              'NANOCLAW_NO_DIAGNOSTICS')
IDENTITY_KEYS = ('SIGNAL_ACCOUNT', 'TELEGRAM_BOT_TOKEN')
BROKER_INPUTS = {'private/mail-config': 'INFOMANIAK_BROKER_CONFIG_FILE',
                 'private/calendar-config': 'NEXTCLOUD_BROKER_CONFIG_FILE'}
EXPORT_ID = re.compile(r'[0-9a-f]{8}-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}\Z')
MODES = ('rehearsal', 'migration')


class PortableError(Exception):
    pass


def fail(code):
    raise PortableError(code)


def require(condition, code):
    if not condition:
        fail(code)


def now_stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def excluded(name):
    return any(name == prefix or name.startswith(prefix + '/') for prefix in EXCLUDED)


def migrations_of(db_path):
    with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=30) as db:
        return sorted(row[0] for row in db.execute('SELECT name FROM schema_version'))


def counts_of(db_path):
    return LEGACY.db_counts(db_path)


def filter_archive(source, destination):
    """Copy a checked backup archive without the machine-owned members."""
    kept = set()
    with tarfile.open(source, 'r:') as inp, tarfile.open(destination, 'w:', format=tarfile.PAX_FORMAT) as out:
        for item in inp:
            if excluded(item.name):
                continue
            if item.islnk():
                require(item.linkname in kept, 'export_hardlink_target_excluded')
            out.addfile(item, inp.extractfile(item) if item.isfile() else None)
            kept.add(item.name)
    RECOVERY.checked_members(destination)


# ---------------------------------------------------------------- export

def export(args):
    folder = RECOVERY.private_directory(Path(args.backup_dir))
    backup_key = RECOVERY.source_path(args.backup_key, kind='file')
    export_root = RECOVERY.private_directory(Path(args.export_root))
    key_root = RECOVERY.private_directory(Path(args.key_root))
    for path in (export_root, key_root):
        RECOVERY.not_nested(path, folder)
    RECOVERY.not_nested(export_root, key_root)
    manifest = RECOVERY.authenticated_manifest(folder, backup_key)
    size = int(manifest['state']['bytes']) + int(manifest['postgres']['bytes'])
    require(shutil.disk_usage(export_root).free > 3 * size + (256 << 20), 'export_space_insufficient')
    identifier = f'{manifest["revision"][:8]}-{now_stamp()}-{secrets.token_hex(3)}'
    bundle = export_root / f'{identifier}.ncx'
    key = key_root / f'{identifier}.key'
    with tempfile.TemporaryDirectory(prefix='.portable-export-', dir=export_root) as temp:
        work = Path(temp)
        os.chmod(work, 0o700)
        archive, dump = RECOVERY.decrypt_checked(folder, backup_key, manifest, work)
        with tarfile.open(archive, 'r:') as stream:
            database = work / 'v2.db'
            with database.open('wb') as output:
                shutil.copyfileobj(stream.extractfile('state/data/v2.db'), output)
            release = json.load(stream.extractfile('state/release.json'))
        filtered = work / 'portable.tar'
        filter_archive(archive, filtered)
        archive.unlink()
        with key.open('x') as stream:
            stream.write(secrets.token_hex(32) + '\n')
        os.chmod(key, 0o600)
        try:
            payload = {
                'schema': SCHEMA, 'revision': manifest['revision'], 'version': release.get('version'),
                'created_utc': now_stamp(), 'migrations': migrations_of(database), 'counts': counts_of(database),
                'state': RECOVERY.encrypt(filtered, work / 'state.tar.enc', key),
                'postgres': RECOVERY.encrypt(dump, work / 'postgres.dump.enc', key),
            }
            payload['hmac_sha256'] = RECOVERY.manifest_mac(payload, key)
            (work / 'manifest.json').write_text(json.dumps(payload, sort_keys=True, indent=2) + '\n')
            partial = export_root / f'.{identifier}.ncx.partial'
            with tarfile.open(partial, 'w:', format=tarfile.PAX_FORMAT) as out:
                for name in PARTS:
                    info = out.gettarinfo(str(work / name), arcname=name)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    info.mode = 0o600
                    with (work / name).open('rb') as stream:
                        out.addfile(info, stream)
            os.chmod(partial, 0o600)
            os.replace(partial, bundle)
        except BaseException:
            key.unlink(missing_ok=True)
            (export_root / f'.{identifier}.ncx.partial').unlink(missing_ok=True)
            raise
    print('export=created', flush=True)
    print(f'export_id={identifier}', flush=True)
    return identifier


# ---------------------------------------------------------------- import

def open_bundle(bundle, key, work):
    """Unpack the three parts into `work`, then authenticate the manifest. Returns it."""
    info = bundle.lstat()
    require(bundle.is_file() and not bundle.is_symlink() and info.st_nlink == 1, 'bundle_unsafe')
    require(shutil.disk_usage(work).free > 3 * info.st_size + (256 << 20), 'import_space_insufficient')
    seen = set()
    with tarfile.open(bundle, 'r:') as stream:
        for item in stream:
            require(item.isfile() and item.name in PARTS and item.name not in seen, 'bundle_member_unexpected')
            require(item.name != 'manifest.json' or item.size <= 64 * 1024, 'bundle_member_unexpected')
            seen.add(item.name)
            with (work / item.name).open('xb') as output:
                shutil.copyfileobj(stream.extractfile(item), output)
    require(seen == set(PARTS), 'bundle_member_missing')
    manifest = json.loads((work / 'manifest.json').read_text())
    require(manifest.get('schema') == SCHEMA, 'bundle_schema_invalid')
    mac = manifest.pop('hmac_sha256', None)
    require(isinstance(mac, str) and RECOVERY.hmac.compare_digest(mac, RECOVERY.manifest_mac(manifest, key)),
            'bundle_authentication_failed')
    require(re.fullmatch(r'[0-9a-f]{40}', str(manifest.get('revision', ''))), 'bundle_schema_invalid')
    return manifest


def target_is_empty(db_path):
    with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=30) as db:
        return all(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
                   for table in ('agent_groups', 'messaging_groups'))


def merge_env(target_text, source_values, mode):
    """The target's `.env`, with the carried keys (and identities in a migration) from the source."""
    carried = {k: source_values[k] for k in CARRY_KEYS if source_values.get(k)}
    if mode == 'migration':
        carried.update({k: source_values[k] for k in IDENTITY_KEYS if source_values.get(k)})
    for value in carried.values():
        require('\n' not in value and '\r' not in value, 'environment_value_invalid')
    output, seen = [], set()
    for line in target_text.splitlines():
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else None
        if key in carried:
            if key not in seen:
                output.append(f'{key}={carried[key]}')
                seen.add(key)
            continue
        if mode == 'rehearsal' and key in IDENTITY_KEYS:
            continue  # a test copy never carries a real channel identity
        output.append(line)
    output.extend(f'{key}={value}' for key, value in carried.items() if key not in seen)
    merged = '\n'.join(output) + '\n'
    if mode == 'rehearsal':
        require(not any(LEGACY.parse_env(merged).get(k) for k in IDENTITY_KEYS), 'rehearsal_identity_present')
    return merged


def is_ancestor(project, older, newer):
    result = subprocess.run(['git', '-c', f'safe.directory={project}', '-C', str(project), 'merge-base',
                             '--is-ancestor', older, newer], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
    return result.returncode == 0


def preflight(args):
    project = RECOVERY.source_path(args.project_root, kind='dir')
    state = RECOVERY.source_path(args.state_root, kind='dir')
    bundle = RECOVERY.source_path(args.bundle, kind='file')
    key = RECOVERY.source_path(args.key_file, kind='file')
    work = RECOVERY.private_directory(Path(args.work_root))
    backup = RECOVERY.private_directory(Path(args.target_backup_dir))
    backup_key = RECOVERY.source_path(args.target_backup_key, kind='file')
    for path in (bundle.parent, key.parent, work, backup, backup_key.parent):
        RECOVERY.not_nested(path, project)
        RECOVERY.not_nested(path, state)
    require(not key.stat().st_mode & 0o077 and key.stat().st_uid == 0, 'key_unsafe')
    require(work.stat().st_dev == state.stat().st_dev, 'work_root_cross_filesystem')

    env_file = project / '.env'
    target_env = env_file.read_text()
    target_values = LEGACY.parse_env(target_env)
    release = json.loads((state / 'release.json').read_text())
    marker = json.loads((state / 'data/upgrade-state.json').read_text())
    require(marker.get('commit') == release.get('revision'), 'target_release_inconsistent')
    _, head = LEGACY.run(['git', '-c', f'safe.directory={project}', '-C', str(project), 'rev-parse', 'HEAD'])
    require(head == release.get('revision'), 'target_checkout_mismatch')
    backup_manifest = json.loads((backup / 'manifest.json').read_text())
    require(backup_manifest.get('revision') == release['revision'], 'target_backup_release_mismatch')
    RECOVERY.verify_or_stage(argparse.Namespace(action='verify', backup_dir=str(backup), key_file=str(backup_key)))
    require(LEGACY.services_healthy(project), 'target_services_unhealthy')
    empty = target_is_empty(state / 'data/v2.db')
    target_migrations = set(migrations_of(state / 'data/v2.db'))

    txn = work / f'{now_stamp()}-{secrets.token_hex(3)}'
    txn.mkdir(mode=0o700)
    plain = txn / 'plain'
    plain.mkdir(mode=0o700)
    try:
        manifest = open_bundle(bundle, key, plain)
        # Same release or a newer one: the target host migrates older data at start.
        require(manifest['revision'] == release['revision'] or
                is_ancestor(project, manifest['revision'], release['revision']), 'bundle_release_newer_than_target')
        require(set(manifest['migrations']) <= target_migrations, 'bundle_schema_newer_than_target')
        archive, dump = plain / 'state.tar', plain / 'postgres.dump'
        RECOVERY.decrypt(plain / 'state.tar.enc', archive, key, manifest['state'])
        RECOVERY.decrypt(plain / 'postgres.dump.enc', dump, key, manifest['postgres'])
        (plain / 'state.tar.enc').unlink()
        (plain / 'postgres.dump.enc').unlink()
        RECOVERY.checked_members(archive)
        with tarfile.open(archive, 'r:') as stream:
            source_env = LEGACY.parse_env(stream.extractfile('env').read().decode('utf-8', 'replace'))
            brokers = {name: stream.extractfile(name).read() for name in BROKER_INPUTS}
        merged_env = merge_env(target_env, source_env, args.mode)
        if args.mode == 'migration':
            require(any(source_env.get(k) for k in IDENTITY_KEYS), 'migration_identity_missing')
        broker_targets = {}
        for name, variable in BROKER_INPUTS.items():
            path = Path(target_values.get(variable, ''))
            require(path.is_absolute() and path.is_file() and not path.is_symlink(), 'target_broker_config_missing')
            broker_targets[name] = path
        require(shutil.disk_usage(state).free > 2 * archive.stat().st_size + (1 << 30), 'target_space_insufficient')
        print('import_preflight=ok', flush=True)
        print(f'import_mode={args.mode}', flush=True)
        print(f'import_target={"empty" if empty else "populated"}', flush=True)
        print(f'import_release={"same" if manifest["revision"] == release["revision"] else "older_migrates_at_start"}',
              flush=True)
        return dict(project=project, state=state, txn=txn, plain=plain, archive=archive, dump=dump,
                    manifest=manifest, merged_env=merged_env, env_file=env_file, brokers=brokers,
                    broker_targets=broker_targets, empty=empty,
                    target_install=target_values.get('NANOCLAW_INSTALL_ID', ''))
    except BaseException:
        shutil.rmtree(txn, ignore_errors=True)  # never leave plaintext behind
        raise


def apply_import(ctx, mode):
    project, state, txn = ctx['project'], ctx['state'], ctx['txn']
    phase = 'stop_stack'
    try:
        volumes = LEGACY.volume_names(project)
        LEGACY.stop_stack(project, ctx['target_install'])
        print('target_stack_stopped=yes', flush=True)

        phase = 'set_aside_current_state'
        previous = txn / 'previous'
        previous.mkdir(mode=0o700)
        marker = (state / 'data/upgrade-state.json').read_bytes()
        for entry in STATE_ENTRIES:
            if (state / entry).exists() or (state / entry).is_symlink():
                os.replace(state / entry, previous / entry)
        shutil.copy2(ctx['env_file'], previous / 'env')
        for name, path in ctx['broker_targets'].items():
            shutil.copy2(path, previous / name.replace('private/', ''))

        phase = 'install_state'
        stage = txn / 'stage'
        stage.mkdir(mode=0o700)
        RECOVERY.extract_checked(ctx['archive'], stage, ['state', 'onecli-data'])
        source = stage / 'state'
        for entry in ('data', 'groups'):
            os.replace(source / entry, state / entry)
        if mode == 'migration' and (source / 'signal').is_dir():
            os.replace(source / 'signal', state / 'signal')
        else:
            (state / 'signal').mkdir(mode=0o700)
            (state / 'signal/attachments').mkdir(mode=0o700)
        for entry in ('store', 'signal-outbox'):
            if (source / entry).is_dir():
                os.replace(source / entry, state / entry)
            else:
                (state / entry).mkdir(mode=0o700)
        # The target keeps its own release marker and machine-owned data.
        (state / 'data/upgrade-state.json').write_bytes(marker)
        for name in PRESERVED_DATA:
            kept = previous / 'data' / name
            if kept.exists() and not kept.is_symlink():
                os.replace(kept, state / 'data' / name)

        phase = 'fix_central_db'
        with sqlite3.connect(state / 'data/v2.db', timeout=30) as db:
            forgotten = db.execute(
                "UPDATE sessions SET container_status = 'stopped' WHERE container_status = 'running'").rowcount
        paused = closed = 0
        if mode == 'rehearsal':
            for inbound in (state / 'data/v2-sessions').glob('*/*/inbound.db'):
                tasks, chats = LEGACY.neutralize_session(inbound)
                paused += tasks
                closed += chats
        for entry in STATE_ENTRIES:
            LEGACY.chown_tree(state / entry)
        os.chmod(state / 'data/upgrade-state.json', 0o600)
        print(f'stale_containers_forgotten={forgotten}', flush=True)
        if mode == 'rehearsal':
            print(f'tasks_paused={paused}', flush=True)
            print(f'pending_chat_closed={closed}', flush=True)

        phase = 'brokers'
        for name, path in ctx['broker_targets'].items():
            LEGACY.atomic_write(path, ctx['brokers'][name], LEGACY.SERVICE_UID, LEGACY.SERVICE_GID, 0o600)

        phase = 'environment'
        info = ctx['env_file'].stat()
        LEGACY.atomic_write(ctx['env_file'], ctx['merged_env'].encode(), info.st_uid, info.st_gid, 0o600)

        phase = 'onecli_volumes'
        LEGACY.restore_onecli(project, ctx['dump'], stage / 'onecli-data', volumes)
        print('onecli_state_restored=yes', flush=True)

        phase = 'start_stack'
        LEGACY.compose(project, 'up', '-d', '--wait')
        require(LEGACY.services_healthy(project), 'imported_stack_unhealthy')

        phase = 'verify'
        require(counts_of(state / 'data/v2.db') == ctx['manifest']['counts'], 'imported_counts_mismatch')
        print('import_counts=match', flush=True)
        print('import=healthy', flush=True)
    except Exception:
        print(f'import_failed_phase={phase}', flush=True)
        print(f'import_transaction={txn.name}', flush=True)
        print('rollback=available_with_--rollback-txn', flush=True)
        raise
    finally:
        shutil.rmtree(ctx['plain'], ignore_errors=True)
        shutil.rmtree(txn / 'stage', ignore_errors=True)


ROLLBACK_REQUIRED = ('data', 'groups', 'env', 'mail-config', 'calendar-config')


def rollback(args):
    """Return the target to the state an import transaction set aside."""
    project = RECOVERY.source_path(args.project_root, kind='dir')
    state = RECOVERY.source_path(args.state_root, kind='dir')
    txn = RECOVERY.private_directory(Path(args.rollback_txn))
    backup = RECOVERY.private_directory(Path(args.target_backup_dir))
    backup_key = RECOVERY.source_path(args.target_backup_key, kind='file')
    for path in (txn, backup, backup_key.parent):
        RECOVERY.not_nested(path, project)
        RECOVERY.not_nested(path, state)
    require(txn.stat().st_dev == state.stat().st_dev, 'work_root_cross_filesystem')
    previous = txn / 'previous'
    require(previous.is_dir() and not previous.is_symlink() and
            all((previous / name).exists() for name in ROLLBACK_REQUIRED), 'previous_state_incomplete')
    release = json.loads((state / 'release.json').read_text())
    marker = json.loads((previous / 'data/upgrade-state.json').read_text())
    require(marker.get('commit') == release.get('revision'), 'previous_state_release_mismatch')
    backup_manifest = json.loads((backup / 'manifest.json').read_text())
    require(backup_manifest.get('revision') == release['revision'], 'target_backup_release_mismatch')
    previous_env = LEGACY.parse_env((previous / 'env').read_text())
    targets = {name: Path(previous_env.get(variable, '')) for name, variable in BROKER_INPUTS.items()}
    require(all(path.is_absolute() for path in targets.values()), 'target_broker_config_missing')
    volumes = LEGACY.volume_names(project)
    stage = txn / 'rollback-stage'
    require(not stage.exists() and not stage.is_symlink(), 'rollback_stage_exists')
    try:
        RECOVERY.verify_or_stage(argparse.Namespace(action='stage', backup_dir=str(backup), key_file=str(backup_key),
                                                    target_dir=str(stage), confirm_sensitive_plaintext=True))
        require((stage / 'postgres.dump').is_file() and (stage / 'onecli-data').is_dir(), 'target_backup_incomplete')
        print('rollback_preflight=ok', flush=True)
        if not args.apply:
            print('rollback_mutation=disabled', flush=True)
            return
        require(args.confirm_restore_previous_state, 'rollback_confirmation_required')
        phase = 'stop_stack'
        try:
            LEGACY.stop_stack(project, previous_env.get('NANOCLAW_INSTALL_ID', ''))
            phase = 'set_aside_imported_state'
            discarded = txn / 'discarded'
            discarded.mkdir(mode=0o700, exist_ok=True)
            for entry in STATE_ENTRIES:
                if (state / entry).exists() or (state / entry).is_symlink():
                    require(not (discarded / entry).exists(), 'discarded_state_exists')
                    os.replace(state / entry, discarded / entry)
            phase = 'restore_previous_state'
            for entry in STATE_ENTRIES:
                if (previous / entry).exists():
                    os.replace(previous / entry, state / entry)
            phase = 'restore_configuration'
            env_file = project / '.env'
            info = env_file.stat()
            LEGACY.atomic_write(env_file, (previous / 'env').read_bytes(), info.st_uid, info.st_gid, 0o600)
            for name, path in targets.items():
                LEGACY.atomic_write(path, (previous / name.replace('private/', '')).read_bytes(),
                                    LEGACY.SERVICE_UID, LEGACY.SERVICE_GID, 0o600)
            phase = 'onecli_volumes'
            LEGACY.restore_onecli(project, stage / 'postgres.dump', stage / 'onecli-data', volumes)
            phase = 'start_stack'
            LEGACY.compose(project, 'up', '-d', '--wait')
            require(LEGACY.services_healthy(project), 'restored_stack_unhealthy')
            print('rollback=healthy', flush=True)
            print('imported_state=retained_in_transaction', flush=True)
        except Exception:
            print(f'rollback_failed_phase={phase}', flush=True)
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main(argv=None):
    parser = RECOVERY.PrivateArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    out = sub.add_parser('export')
    for flag in ('backup-dir', 'backup-key', 'export-root', 'key-root'):
        out.add_argument('--' + flag, required=True)
    inp = sub.add_parser('import')
    for flag in ('project-root', 'state-root', 'target-backup-dir', 'target-backup-key'):
        inp.add_argument('--' + flag, required=True)
    for flag in ('bundle', 'key-file', 'work-root', 'rollback-txn'):
        inp.add_argument('--' + flag)
    inp.add_argument('--mode', choices=MODES)
    inp.add_argument('--confirm-replace-target-state', action='store_true')
    inp.add_argument('--confirm-source-stopped', action='store_true')
    inp.add_argument('--confirm-restore-previous-state', action='store_true')
    inp.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    if args.action == 'export':
        export(args)
        return
    if args.rollback_txn:
        require(not (args.bundle or args.key_file or args.work_root or args.mode), 'invalid_arguments')
        rollback(args)
        return
    require(args.bundle and args.key_file and args.work_root and args.mode, 'invalid_arguments')
    ctx = preflight(args)
    try:
        if not args.apply:
            print('import_mutation=disabled', flush=True)
            return
        require(ctx['empty'] or args.confirm_replace_target_state, 'replace_confirmation_required')
        # Two instances must never run the same channel identities.
        require(args.mode != 'migration' or args.confirm_source_stopped, 'source_stopped_confirmation_required')
        apply_import(ctx, args.mode)
    finally:
        shutil.rmtree(ctx['plain'], ignore_errors=True)
        if not args.apply:
            shutil.rmtree(ctx['txn'], ignore_errors=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('portable_command=failed', flush=True)
        known = isinstance(error, (PortableError, RECOVERY.RecoveryError, LEGACY.ImportError_))
        print('failure_category=' + (str(error)[:80] if known else 'unexpected_error'), flush=True)
        sys.exit(2)
