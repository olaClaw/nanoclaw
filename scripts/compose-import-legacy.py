#!/usr/bin/env python3
"""Import a checkout-based NanoClaw install into a Compose host.

Operator-run, as root, on the Compose host. Input is an encrypted snapshot made
by `legacy-snapshot.py` on the old machine, plus its separate key.

Two modes:
  rehearsal  test copy: pending tasks are paused, pending chat is closed, the
             Signal account state is NOT installed and the target `.env` must
             not carry Signal or Telegram identities, so the copy can never
             talk to real contacts. The mail broker uses the real (read-only)
             credentials from the snapshot.
  cutover    the real move: queues, tasks and Signal state are kept, and the
             Signal/Telegram identities are carried into the target `.env`.
             Only run it while the old install is stopped for good.

Without `--apply` this is a preflight: it authenticates and unpacks the
snapshot into a root-only work area, checks the target and removes the
plaintext again. `--apply` stops the Compose stack, sets the current state
aside inside the work area, installs the imported state, recreates the OneCLI
and PostgreSQL volumes from the snapshot and starts the stack. The target must
have a verified `compose-recovery.py` backup of its current release; recreating
the OneCLI volumes is only reversible through that backup.

Only fixed status labels are printed.
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
from pathlib import Path, PurePosixPath


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECOVERY = _load('compose_recovery', 'compose-recovery.py')
SNAPSHOT = _load('legacy_snapshot', 'legacy-snapshot.py')

MAIL_SERVER = 'infomaniak_mail_readonly'
COMPOSE_BROKER_URL = 'http://infomaniak-mail:18765/mcp'
COMPOSE_BROKER_BIND, COMPOSE_BROKER_PORT = '0.0.0.0', 18765
CARRY_KEYS = ('OPENCODE_BASE_URL', 'OPENCODE_MODEL', 'OPENCODE_PROVIDER', 'OPENCODE_SMALL_MODEL',
              'DEFAULT_AGENT_PROVIDER', 'TZ', 'NANOCLAW_NO_DIAGNOSTICS')
IDENTITY_KEYS = ('SIGNAL_ACCOUNT', 'TELEGRAM_BOT_TOKEN')
STATE_ENTRIES = ('data', 'groups', 'store', 'signal', 'signal-outbox')
SERVICE_UID = SERVICE_GID = 1000
REQUIRED = {'data': 'dir', 'data/v2.db': 'file', 'groups': 'dir', 'env': 'file', 'signal': 'dir',
            'onecli-data': 'dir'}
OPTIONAL = {'config-nanoclaw': 'dir', 'mail-config': 'file'}
LINK_ROOTS = ('data/', 'groups/')


class ImportError_(Exception):
    pass


def fail(code):
    raise ImportError_(code)


def require(condition, code):
    if not condition:
        fail(code)


# ---------------------------------------------------------------- pure helpers

def check_members(archive):
    """Validate member names and types; return {name: kind}."""
    kinds, links = {}, set()
    with tarfile.open(archive, 'r:') as stream:
        for item in stream:
            path = PurePosixPath(item.name)
            name = str(path)
            require(not item.name.startswith('/') and path.parts and '..' not in path.parts and
                    name == item.name.rstrip('/') and name not in kinds, 'archive_member_unsafe')
            require(not any(str(parent) in links for parent in path.parents), 'archive_member_unsafe')
            top = path.parts[0]
            require(top in REQUIRED or top in OPTIONAL, 'archive_member_unexpected')
            if item.issym():
                # Links inside group workspaces are interpreted inside the agent
                # container (e.g. a venv's python); they are recreated verbatim
                # and never followed during import.
                require(name.startswith(LINK_ROOTS), 'archive_link_unexpected')
                links.add(name)
                kinds[name] = 'link'
            elif item.islnk():
                require(RECOVERY.hardlink_target_ok(item, kinds), 'archive_member_unsafe')
                kinds[name] = 'hardlink'
            elif item.isfile():
                kinds[name] = 'file'
            elif item.isdir():
                kinds[name] = 'dir'
            else:
                fail('archive_member_unsafe')
    for name, kind in REQUIRED.items():
        require(kinds.get(name) == kind, 'archive_members_missing')
    for name, kind in OPTIONAL.items():
        require(kinds.get(name, kind) == kind, 'archive_member_unsafe')
    return kinds


def rewrite_mcp_servers(raw):
    """Point the mail broker MCP server at the Compose service. Returns (json, changed)."""
    if not raw:
        return raw, 0
    servers = json.loads(raw)
    if not isinstance(servers, dict):
        return raw, 0
    changed = 0
    for name, cfg in servers.items():
        if name == MAIL_SERVER and isinstance(cfg, dict) and cfg.get('type') == 'http':
            if cfg.get('url') != COMPOSE_BROKER_URL:
                cfg['url'] = COMPOSE_BROKER_URL
                changed += 1
    return (json.dumps(servers, separators=(',', ':')) if changed else raw), changed


def translate_mail_config(raw, legacy_root, state_root):
    """Same credentials and token; listen inside the broker container; move downloads under the state root."""
    required = ('email_address', 'device_password', 'broker_token', 'download_dir')
    require(isinstance(raw, dict) and all(isinstance(raw.get(k), str) and raw.get(k) for k in required),
            'mail_config_invalid')
    download = PurePosixPath(raw['download_dir'])
    legacy = PurePosixPath(legacy_root)
    require(download.is_absolute() and download.is_relative_to(legacy), 'mail_download_dir_outside_install')
    relative = download.relative_to(legacy)
    require(relative.parts and relative.parts[0] == 'groups' and '..' not in relative.parts,
            'mail_download_dir_outside_groups')
    result = dict(raw)
    result.update(bind=COMPOSE_BROKER_BIND, port=COMPOSE_BROKER_PORT,
                  download_dir=str(PurePosixPath(state_root) / relative))
    return result


def parse_env(text):
    values = {}
    for line in text.splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"\'')
    return values


def merge_env(target_text, legacy_values, mode, extra):
    """Carry model/timezone settings (and identities in cutover) into the target .env."""
    carried = {k: legacy_values[k] for k in CARRY_KEYS if legacy_values.get(k)}
    if mode == 'cutover':
        carried.update({k: legacy_values[k] for k in IDENTITY_KEYS if legacy_values.get(k)})
    carried.update(extra)
    for value in carried.values():
        require('\n' not in value and '\r' not in value, 'environment_value_invalid')
    lines = target_text.splitlines()
    seen = set()
    output = []
    for line in lines:
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else None
        if key in carried:
            if key not in seen:
                output.append(f'{key}={carried[key]}')
                seen.add(key)
            continue
        if mode == 'rehearsal' and key in IDENTITY_KEYS:
            continue  # a test copy must never carry a real channel identity
        output.append(line)
    for key, value in carried.items():
        if key not in seen:
            output.append(f'{key}={value}')
    merged = '\n'.join(output) + '\n'
    if mode == 'rehearsal':
        values = parse_env(merged)
        require(not any(values.get(k) for k in IDENTITY_KEYS), 'rehearsal_identity_present')
    return merged


def neutralize_session(inbound):
    """Rehearsal only: pause pending tasks, close pending chat. Returns (tasks, chats)."""
    with sqlite3.connect(inbound, timeout=30) as db:
        tasks = db.execute("UPDATE messages_in SET status = 'paused' WHERE kind = 'task' AND status = 'pending'").rowcount
        chats = db.execute("UPDATE messages_in SET status = 'completed' WHERE kind != 'task' AND status = 'pending'").rowcount
    return tasks, chats


def fix_central_db(path):
    """Rewrite the mail MCP URL and forget containers of the old host. Returns counts."""
    with sqlite3.connect(path, timeout=30) as db:
        rewritten = 0
        for group, raw in db.execute('SELECT agent_group_id, mcp_servers FROM container_configs').fetchall():
            new, changed = rewrite_mcp_servers(raw)
            if changed:
                db.execute('UPDATE container_configs SET mcp_servers = ? WHERE agent_group_id = ?', (new, group))
                rewritten += changed
        stopped = db.execute("UPDATE sessions SET container_status = 'stopped' WHERE container_status = 'running'").rowcount
    return rewritten, stopped


def db_counts(path):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30) as db:
        return {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in SNAPSHOT.COUNT_TABLES}


# ---------------------------------------------------------------- side effects

def run(argv, *, cwd=None, stdin=None, check=True, timeout=900):
    result = subprocess.run(argv, cwd=cwd, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    if check and result.returncode:
        fail('command_failed')
    return result.returncode, result.stdout.decode('utf-8', 'replace').strip()


def compose(project, *args, **kwargs):
    return run(['docker', 'compose', '-f', str(project / 'compose.yaml'), '--profile', RECOVERY.PROFILE, *args],
               cwd=project, **kwargs)


def extract(archive, target):
    """Extract regular files and directories with numeric owners, then recreate links verbatim."""
    with tarfile.open(archive, 'r:') as stream:
        members = stream.getmembers()
        links = [m for m in members if m.issym()]
        stream.extractall(target, members=[m for m in members if not m.issym()],
                          filter=RECOVERY.preserve_numeric_metadata)
    root = target.resolve(strict=True)
    for item in links:
        destination = target / item.name
        require(not destination.exists() and not destination.is_symlink() and destination.parent.is_dir(),
                'archive_member_unsafe')
        require(destination.parent.resolve(strict=True).is_relative_to(root), 'archive_member_unsafe')
        os.symlink(item.linkname, destination)


def chown_tree(path):
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in [*dirs, *files]:
            os.lchown(os.path.join(root, name), SERVICE_UID, SERVICE_GID)
    os.lchown(path, SERVICE_UID, SERVICE_GID)


def atomic_write(path, content, uid, gid, mode):
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.import-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def volume_names(project):
    """Docker names of the OneCLI data and PostgreSQL volumes, from the resolved Compose config."""
    _, raw = compose(project, 'config', '--format', 'json')
    volumes = json.loads(raw).get('volumes', {})
    names = tuple((volumes.get(key) or {}).get('name') for key in ('onecli-data', 'onecli-pgdata'))
    require(all(isinstance(n, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', n) for n in names),
            'volume_names_unresolved')
    return names


def restore_onecli(project, dump, onecli_source, volumes):
    """Recreate the OneCLI and PostgreSQL volumes from a dump and a copy of OneCLI /app/data."""
    onecli_volume, _ = volumes
    compose(project, 'rm', '-sf', 'onecli-egress', 'onecli', 'postgres')
    for volume in volumes:
        if run(['docker', 'volume', 'inspect', volume], check=False)[0] == 0:
            run(['docker', 'volume', 'rm', volume])
    compose(project, 'up', '-d', '--wait', '--no-deps', 'postgres')
    with dump.open('rb') as stream:
        compose(project, 'exec', '-T', 'postgres', 'pg_restore', '--no-owner', '--no-privileges',
                '-U', 'onecli', '-d', 'onecli', stdin=stream)
    # `create` has no --no-deps; --no-recreate leaves the running PostgreSQL alone.
    compose(project, 'create', '--no-recreate', 'onecli')
    _, mountpoint = run(['docker', 'volume', 'inspect', '-f', '{{.Mountpoint}}', onecli_volume])
    target = RECOVERY.source_path(mountpoint, kind='dir')
    run(['cp', '-a', str(onecli_source) + '/.', str(target) + '/'])


def stop_stack(project, install_id):
    compose(project, 'stop')
    if install_id:
        _, agents = run(['docker', 'ps', '-q', '--filter', f'label=nanoclaw-install={install_id}'])
        if agents:
            run(['docker', 'stop', *agents.split()])


def services_healthy(project):
    _, raw = compose(project, 'ps', '--all', '--format', 'json')
    records = json.loads(raw) if raw.lstrip().startswith('[') else [json.loads(l) for l in raw.splitlines() if l]
    return bool(records) and all(r.get('State') == 'running' and r.get('Health') in ('healthy', '')
                                 for r in records)


# ---------------------------------------------------------------- transaction

def preflight(args):
    project = RECOVERY.source_path(args.project_root, kind='dir')
    state = RECOVERY.source_path(args.state_root, kind='dir')
    snapshot = RECOVERY.private_directory(Path(args.snapshot_dir))
    key = RECOVERY.source_path(args.key_file, kind='file')
    work = RECOVERY.private_directory(Path(args.work_root))
    backup = RECOVERY.private_directory(Path(args.target_backup_dir))
    backup_key = RECOVERY.source_path(args.target_backup_key, kind='file')
    for path in (snapshot, key.parent, work, backup, backup_key.parent):
        RECOVERY.not_nested(path, project)
        RECOVERY.not_nested(path, state)
    RECOVERY.not_nested(work, snapshot)
    RECOVERY.not_nested(snapshot, key.parent)
    require(not key.stat().st_mode & 0o077 and key.stat().st_uid == 0, 'key_unsafe')
    require(work.stat().st_dev == state.stat().st_dev, 'work_root_cross_filesystem')

    manifest = SNAPSHOT.verify(snapshot, key)
    env_file = project / '.env'
    target_env = env_file.read_text()
    target_values = parse_env(target_env)

    # Target identity: release, marker, checkout agree; verified backup of it.
    release = json.loads((state / 'release.json').read_text())
    marker = json.loads((state / 'data/upgrade-state.json').read_text())
    require(marker.get('commit') == release.get('revision'), 'target_release_inconsistent')
    _, head = run(['git', '-c', f'safe.directory={project}', '-C', str(project), 'rev-parse', 'HEAD'])
    require(head == release.get('revision'), 'target_checkout_mismatch')
    backup_manifest = json.loads((backup / 'manifest.json').read_text())
    require(backup_manifest.get('revision') == release['revision'], 'target_backup_release_mismatch')
    RECOVERY.verify_or_stage(argparse.Namespace(action='verify', backup_dir=str(backup), key_file=str(backup_key)))
    require(services_healthy(project), 'target_services_unhealthy')
    with sqlite3.connect(f'file:{state / "data/v2.db"}?mode=ro', uri=True) as db:
        target_migrations = sorted(r[0] for r in db.execute('SELECT name FROM schema_version'))
    require(manifest['migrations'] == target_migrations, 'schema_mismatch')

    mail_target = Path(target_values.get('INFOMANIAK_BROKER_CONFIG_FILE', ''))
    require(mail_target.is_absolute() and mail_target.is_file() and not mail_target.is_symlink(),
            'target_mail_config_missing')

    txn = work / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3))
    txn.mkdir(mode=0o700)
    plain = txn / 'plain'
    plain.mkdir(mode=0o700)
    archive, dump = plain / 'state.tar', plain / 'postgres.dump'
    try:
        return _unpacked_preflight(args, project, state, snapshot, key, manifest, txn, plain, archive, dump,
                                   env_file, target_env, target_values, mail_target)
    except BaseException:
        shutil.rmtree(txn, ignore_errors=True)  # never leave snapshot plaintext behind
        raise


def _unpacked_preflight(args, project, state, snapshot, key, manifest, txn, plain, archive, dump,
                        env_file, target_env, target_values, mail_target):
    RECOVERY.decrypt(snapshot / 'state.tar.enc', archive, key, manifest['state'])
    RECOVERY.decrypt(snapshot / 'postgres.dump.enc', dump, key, manifest['postgres'])
    kinds = check_members(archive)
    with tarfile.open(archive, 'r:') as stream:
        legacy_env = parse_env(stream.extractfile('env').read().decode('utf-8', 'replace'))
        mail_raw = json.load(stream.extractfile('mail-config')) if 'mail-config' in kinds else None
    require(mail_raw is not None, 'snapshot_mail_config_missing')
    mail = translate_mail_config(mail_raw, manifest['legacy_project_root'], str(state))
    extra = {'INFOMANIAK_DOWNLOAD_DIR': mail['download_dir']}
    merged_env = merge_env(target_env, legacy_env, args.mode, extra)
    if args.mode == 'cutover':
        require(legacy_env.get('SIGNAL_ACCOUNT'), 'cutover_signal_identity_missing')
    free = shutil.disk_usage(state).free
    require(free > 2 * archive.stat().st_size + (1 << 30), 'target_space_insufficient')
    print('import_preflight=ok', flush=True)
    print(f'import_mode={args.mode}', flush=True)
    return dict(project=project, state=state, txn=txn, plain=plain, archive=archive, dump=dump,
                manifest=manifest, mail=mail, merged_env=merged_env, env_file=env_file, mail_target=mail_target,
                target_install=target_values.get('NANOCLAW_INSTALL_ID', ''))


def apply_import(ctx, mode):
    project, state, txn = ctx['project'], ctx['state'], ctx['txn']
    phase = 'stop_stack'
    try:
        volumes = volume_names(project)
        stop_stack(project, ctx['target_install'])
        print('target_stack_stopped=yes', flush=True)

        phase = 'set_aside_current_state'
        previous = txn / 'previous'
        previous.mkdir(mode=0o700)
        marker = (state / 'data/upgrade-state.json').read_bytes()
        for entry in STATE_ENTRIES:
            if (state / entry).exists() or (state / entry).is_symlink():
                os.replace(state / entry, previous / entry)
        shutil.copy2(ctx['env_file'], previous / 'env')
        shutil.copy2(ctx['mail_target'], previous / 'mail-config')

        phase = 'install_state'
        stage = txn / 'stage'
        stage.mkdir(mode=0o700)
        extract(ctx['archive'], stage)
        os.replace(stage / 'data', state / 'data')
        os.replace(stage / 'groups', state / 'groups')
        if mode == 'cutover':
            os.replace(stage / 'signal', state / 'signal')
        else:
            (state / 'signal').mkdir(mode=0o700)
            (state / 'signal/attachments').mkdir(mode=0o700)
        for entry in ('signal-outbox', 'store'):
            (state / entry).mkdir(mode=0o700)
        (state / 'data/upgrade-state.json').write_bytes(marker)

        phase = 'fix_central_db'
        rewritten, forgotten = fix_central_db(state / 'data/v2.db')
        paused = closed = 0
        if mode == 'rehearsal':
            for inbound in (state / 'data/v2-sessions').glob('*/*/inbound.db'):
                tasks, chats = neutralize_session(inbound)
                paused += tasks
                closed += chats
        for entry in STATE_ENTRIES:
            chown_tree(state / entry)
        os.chmod(state / 'data/upgrade-state.json', 0o600)
        print(f'mcp_urls_rewritten={rewritten}', flush=True)
        print(f'stale_containers_forgotten={forgotten}', flush=True)
        if mode == 'rehearsal':
            print(f'tasks_paused={paused}', flush=True)
            print(f'pending_chat_closed={closed}', flush=True)

        phase = 'mail_broker'
        download = Path(ctx['mail']['download_dir'])
        download.mkdir(parents=True, exist_ok=True)
        chown_tree(download)
        atomic_write(ctx['mail_target'], (json.dumps(ctx['mail'], indent=2) + '\n').encode(),
                     SERVICE_UID, SERVICE_GID, 0o600)

        phase = 'environment'
        info = ctx['env_file'].stat()
        atomic_write(ctx['env_file'], ctx['merged_env'].encode(), info.st_uid, info.st_gid, 0o600)

        phase = 'onecli_volumes'
        restore_onecli(project, ctx['dump'], stage / 'onecli-data', volumes)
        print('onecli_state_restored=yes', flush=True)

        phase = 'start_stack'
        compose(project, 'up', '-d', '--wait')
        require(services_healthy(project), 'imported_stack_unhealthy')

        phase = 'verify'
        counts = db_counts(state / 'data/v2.db')
        require(counts == ctx['manifest']['counts'], 'imported_counts_mismatch')
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


ROLLBACK_REQUIRED = ('data', 'groups', 'env', 'mail-config')


def rollback(args):
    """Return the target to the state set aside by an import transaction."""
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
    previous_env = parse_env((previous / 'env').read_text())
    mail_target = Path(previous_env.get('INFOMANIAK_BROKER_CONFIG_FILE', ''))
    require(mail_target.is_absolute(), 'target_mail_config_missing')
    volumes = volume_names(project)
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
        _apply_rollback(project, state, txn, previous, previous_env, mail_target, stage, volumes)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _apply_rollback(project, state, txn, previous, previous_env, mail_target, stage, volumes):
    phase = 'stop_stack'
    try:
        stop_stack(project, previous_env.get('NANOCLAW_INSTALL_ID', ''))
        print('target_stack_stopped=yes', flush=True)

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
        atomic_write(env_file, (previous / 'env').read_bytes(), info.st_uid, info.st_gid, 0o600)
        atomic_write(mail_target, (previous / 'mail-config').read_bytes(), SERVICE_UID, SERVICE_GID, 0o600)

        phase = 'onecli_volumes'
        restore_onecli(project, stage / 'postgres.dump', stage / 'onecli-data', volumes)

        phase = 'start_stack'
        compose(project, 'up', '-d', '--wait')
        require(services_healthy(project), 'restored_stack_unhealthy')
        print('rollback=healthy', flush=True)
        print('imported_state=retained_in_transaction', flush=True)
    except Exception:
        print(f'rollback_failed_phase={phase}', flush=True)
        raise


def main():
    parser = RECOVERY.PrivateArgumentParser(description=__doc__)
    for flag in ('project-root', 'state-root', 'target-backup-dir', 'target-backup-key'):
        parser.add_argument('--' + flag, required=True)
    for flag in ('snapshot-dir', 'key-file', 'work-root', 'rollback-txn'):
        parser.add_argument('--' + flag)
    parser.add_argument('--mode', choices=('rehearsal', 'cutover'))
    parser.add_argument('--confirm-replace-target-state', action='store_true')
    parser.add_argument('--confirm-restore-previous-state', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    if args.rollback_txn:
        require(not (args.snapshot_dir or args.key_file or args.work_root or args.mode), 'invalid_arguments')
        rollback(args)
        return
    require(args.snapshot_dir and args.key_file and args.work_root and args.mode, 'invalid_arguments')
    ctx = preflight(args)
    try:
        if not args.apply:
            print('import_mutation=disabled', flush=True)
            return
        require(args.confirm_replace_target_state, 'replace_confirmation_required')
        apply_import(ctx, args.mode)
    finally:
        shutil.rmtree(ctx['plain'], ignore_errors=True)
        if not args.apply:
            shutil.rmtree(ctx['txn'], ignore_errors=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('import_command=failed', flush=True)
        known = isinstance(error, (ImportError_, RECOVERY.RecoveryError, SNAPSHOT.SnapshotError))
        print('failure_category=' + (str(error)[:80] if known else 'unexpected_error'), flush=True)
        sys.exit(2)
