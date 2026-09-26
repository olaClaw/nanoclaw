#!/usr/bin/env python3
"""Guarded synthetic restore rehearsal with unconditional original rollback.

This is not a production migration command. It always returns to the original
state/volumes and retains recovery artifacts for manual inspection.
"""

import argparse
import fcntl
import importlib.util
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone


SPEC = importlib.util.spec_from_file_location(
    'compose_cutover', Path(__file__).with_name('compose-cutover.py'))
CUTOVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CUTOVER)
RECOVERY = CUTOVER.RECOVERY

SERVICES = (*RECOVERY.STOP_SERVICES, 'postgres')
SYNTHETIC_CHANNEL_KEYS = ('TELEGRAM_BOT_TOKEN', 'SIGNAL_ACCOUNT')


def run(argv, *, project=None, stdin=None, timeout=300):
    try:
        result = subprocess.run(argv, cwd=project, stdin=stdin,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        RECOVERY.fail('command_timed_out')
    if result.returncode:
        RECOVERY.fail('command_failed')
    return result.stdout


def compose(project, *args, override=None):
    argv = ['docker', 'compose', '-f', str(project / 'compose.yaml')]
    if override is not None:
        argv.extend(['-f', str(override)])
    return run([*argv, '--profile', RECOVERY.PROFILE, *args], project=project)


def redacted_restored_status(project, override):
    argv = ['docker', 'compose', '-f', str(project / 'compose.yaml'), '-f',
            str(override), '--profile', RECOVERY.PROFILE, 'ps', '--all', '--format', 'json']
    try:
        result = subprocess.run(argv, cwd=project, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=20, check=False)
        if result.returncode:
            print('restored_service_status=unavailable', flush=True)
            return
        raw = result.stdout.decode()
        records = json.loads(raw) if raw.lstrip().startswith('[') else [json.loads(line)
                  for line in raw.splitlines() if line.strip()]
        allowed = set(SERVICES)
        states = {'running', 'exited', 'restarting', 'created', 'paused', 'dead'}
        healths = {'healthy', 'unhealthy', 'starting', ''}
        for record in records:
            service = record.get('Service')
            if service not in allowed:
                continue
            state = record.get('State', '')
            health = record.get('Health', '')
            safe_state = state if state in states else 'unknown'
            safe_health = health if health in healths else 'unknown'
            print(f'restored_service_{service}={safe_state}/{safe_health or "none"}', flush=True)
    except Exception:
        print('restored_service_status=unavailable', flush=True)


def synthetic_config_only(env_file):
    for line in env_file.read_text().splitlines():
        if '=' not in line or line.startswith('#'):
            continue
        key, value = line.split('=', 1)
        if key in SYNTHETIC_CHANNEL_KEYS and value.strip().strip('\"\''):
            RECOVERY.fail('real_channel_identity_present')


def synthetic_state_only(state):
    with sqlite3.connect(f'file:{state / "data/v2.db"}?mode=ro', uri=True) as db:
        channels = {row[0] for row in db.execute(
            'SELECT DISTINCT channel_type FROM messaging_groups')}
    if channels != {'cli'}:
        RECOVERY.fail('non_cli_messaging_group_present')


def mount_details(project, service, destination, override=None):
    container = compose(project, 'ps', '-q', service, override=override).decode().strip()
    if not container:
        RECOVERY.fail('service_missing')
    details = json.loads(run(['docker', 'inspect', container]))[0]
    matches = [mount for mount in details['Mounts'] if mount['Destination'] == destination]
    if len(matches) != 1 or matches[0]['Type'] != 'volume':
        RECOVERY.fail('volume_mount_unexpected')
    volume = matches[0]['Name']
    source = RECOVERY.source_path(matches[0]['Source'], kind='dir')
    expected = RECOVERY.source_path(
        run(['docker', 'volume', 'inspect', '-f', '{{.Mountpoint}}', volume]).decode().strip(),
        kind='dir')
    if source != expected:
        RECOVERY.fail('volume_mount_mismatch')
    return volume, source


def absent_volume(name):
    result = subprocess.run(['docker', 'volume', 'inspect', name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=30, check=False)
    if result.returncode == 0:
        RECOVERY.fail('new_volume_already_exists')


def prompt_ready(state):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(180)
        client.connect(str(state / 'data/cli.sock'))
        message = {'text': 'Synthetic recovery test. Reply with exactly READY and nothing else.'}
        client.sendall((json.dumps(message) + '\n').encode())
        with client.makefile() as stream:
            line = stream.readline()
    if not line or json.loads(line).get('text', '').strip() != 'READY':
        RECOVERY.fail('synthetic_prompt_failed')


def restore_postgres(project, override, dump):
    compose(project, 'up', '-d', '--wait', '--force-recreate', 'postgres', override=override)
    with dump.open('rb') as stream:
        run(['docker', 'compose', '-f', str(project / 'compose.yaml'), '-f', str(override),
             '--profile', RECOVERY.PROFILE, 'exec', '-T', 'postgres', 'pg_restore',
             '--no-owner', '--no-privileges', '-U', 'onecli', '-d', 'onecli'],
            project=project, stdin=stream)
    tables = compose(project, 'exec', '-T', 'postgres', 'psql', '-U', 'onecli', '-d',
                     'onecli', '-At', '-c',
                     "SELECT count(*) FROM pg_tables WHERE schemaname='public'",
                     override=override)
    if int(tables.decode().strip()) < 1:
        RECOVERY.fail('postgres_tables_missing')


def stop_original(project, install):
    compose(project, 'stop', 'nanoclaw')
    agents = RECOVERY.running_agents(install)
    if agents:
        run(['docker', 'stop', *agents])
    compose(project, 'stop', *SERVICES[1:])


def rehearse(args):
    if not args.confirm_synthetic or args.apply == args.preflight:
        RECOVERY.fail('explicit_synthetic_confirmation_required')
    project = RECOVERY.source_path(args.project_root, kind='dir')
    state = RECOVERY.source_path(args.state_root, kind='dir')
    work_root = RECOVERY.private_directory(Path(args.work_root))
    backup = RECOVERY.private_directory(Path(args.backup_dir))
    key = RECOVERY.source_path(args.key_file, kind='file')
    for root in (work_root, backup, key.parent):
        RECOVERY.not_nested(root, project)
        RECOVERY.not_nested(root, state)
    RECOVERY.not_nested(work_root, backup)
    RECOVERY.not_nested(work_root, key.parent)
    if work_root.stat().st_dev != state.stat().st_dev:
        RECOVERY.fail('work_root_cross_filesystem')
    synthetic_config_only(project / '.env')

    lock_path = work_root / '.rehearsal.lock'
    with lock_path.open('a+b') as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            RECOVERY.fail('rehearsal_already_running')

        CUTOVER.preflight(args)
        synthetic_state_only(state)
        install, _ = RECOVERY.read_env_paths(project / '.env')
        old_onecli, _ = mount_details(project, 'onecli', '/app/data')
        old_pg, _ = mount_details(project, 'postgres', '/var/lib/postgresql')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        identifier = stamp + '-' + secrets.token_hex(3)
        transaction = work_root / identifier
        original = work_root / (identifier + '-original-state')
        recovered = work_root / (identifier + '-recovered-state')
        for path in (transaction, original, recovered):
            if path.exists() or path.is_symlink():
                RECOVERY.fail('rehearsal_path_exists')
        new_onecli = 'nanoclaw-rehearsal-onecli-' + identifier.lower()
        new_pg = 'nanoclaw-rehearsal-pg-' + identifier.lower()
        absent_volume(new_onecli)
        absent_volume(new_pg)
        transaction.mkdir(mode=0o700)
        stage = transaction / 'stage'
        RECOVERY.verify_or_stage(argparse.Namespace(
            action='stage', backup_dir=str(backup), key_file=str(key),
            target_dir=str(stage), confirm_sensitive_plaintext=True))
        CUTOVER.preflight(argparse.Namespace(
            project_root=str(project), state_root=str(state), stage_dir=str(stage)))
        synthetic_state_only(stage / 'state')
        override = transaction / 'volumes.override.yaml'
        override.write_text('volumes:\n  onecli-data:\n    name: ' + new_onecli +
                            '\n  onecli-pgdata:\n    name: ' + new_pg + '\n')
        config = json.loads(compose(project, 'config', '--format', 'json', override=override))
        if (config['volumes']['onecli-data']['name'] != new_onecli or
                config['volumes']['onecli-pgdata']['name'] != new_pg):
            RECOVERY.fail('volume_override_invalid')
        print('rehearsal_preflight=ok', flush=True)
        if args.preflight:
            print('rehearsal_runtime_mutation=disabled', flush=True)
            return

        stop_started = False
        original_moved = False
        recovered_installed = False
        rollback_ok = False
        phase = 'stop_original'
        try:
            stop_started = True
            stop_original(project, install)
            print('original_stopped=yes', flush=True)
            phase = 'swap_state'
            os.replace(state, original)
            original_moved = True
            os.replace(stage / 'state', state)
            recovered_installed = True
            phase = 'create_volumes'
            run(['docker', 'volume', 'create', new_onecli])
            run(['docker', 'volume', 'create', new_pg])
            phase = 'copy_onecli_data'
            new_onecli_path = RECOVERY.source_path(
                run(['docker', 'volume', 'inspect', '-f', '{{.Mountpoint}}',
                     new_onecli]).decode().strip(), kind='dir')
            run(['cp', '-a', str(stage / 'onecli-data') + '/.', str(new_onecli_path) + '/'])
            phase = 'restore_postgres'
            restore_postgres(project, override, stage / 'postgres.dump')
            phase = 'start_restored_stack'
            compose(project, 'up', '-d', '--wait', '--force-recreate', override=override)
            phase = 'verify_restored_volumes'
            if (mount_details(project, 'onecli', '/app/data', override)[0] != new_onecli or
                    mount_details(project, 'postgres', '/var/lib/postgresql', override)[0] != new_pg):
                RECOVERY.fail('restored_volume_mismatch')
            phase = 'prompt_restored'
            prompt_ready(state)
            print('restored_stack=healthy', flush=True)
            print('restored_synthetic_prompt=ready', flush=True)
        except Exception:
            print('restore_failed_phase=' + phase, flush=True)
            redacted_restored_status(project, override)
            raise
        finally:
            if stop_started:
                try:
                    compose(project, 'stop', 'nanoclaw', override=override)
                    agents = RECOVERY.running_agents(install)
                    if agents:
                        run(['docker', 'stop', *agents])
                    compose(project, 'stop', *SERVICES[1:], override=override)
                    if recovered_installed:
                        os.replace(state, recovered)
                    if original_moved:
                        if state.exists() or state.is_symlink():
                            RECOVERY.fail('original_state_target_occupied')
                        os.replace(original, state)
                    compose(project, 'up', '-d', '--wait', '--force-recreate')
                    if (mount_details(project, 'onecli', '/app/data')[0] != old_onecli or
                            mount_details(project, 'postgres', '/var/lib/postgresql')[0] != old_pg):
                        RECOVERY.fail('original_volume_mismatch')
                    prompt_ready(state)
                    rollback_ok = True
                    print('original_rollback=healthy', flush=True)
                except Exception:
                    print('original_rollback=failed_manual_recovery_needed', flush=True)
                    raise
        if rollback_ok:
            print('recovery_rehearsal=ok', flush=True)


def main():
    parser = RECOVERY.PrivateArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--stage-dir', required=True)
    parser.add_argument('--backup-dir', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--work-root', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--confirm-synthetic', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        RECOVERY.fail('root_required')
    rehearse(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('recovery_rehearsal=failed', flush=True)
        code = str(error).split(':', 1)[0][:80]
        print('failure_category=' + code if isinstance(error, RECOVERY.RecoveryError)
              else 'failure_category=unexpected_error', flush=True)
        sys.exit(2)
