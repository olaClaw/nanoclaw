#!/usr/bin/env python3
"""Read-only preflight for a Compose restore rehearsal.

This command does not stop services, create volumes, or replace live files.
Only fixed status labels are printed. A mutating cutover is deliberately absent.
"""

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath


MODULE_PATH = Path(__file__).with_name('compose-recovery.py')
SPEC = importlib.util.spec_from_file_location('compose_recovery', MODULE_PATH)
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)

REQUIRED_DIRECTORIES = ('state', 'onecli-data', 'private', 'private/mail-downloads')
REQUIRED_FILES = ('state/release.json', 'state/data/v2.db',
                  'state/data/upgrade-state.json', 'env', 'postgres.dump',
                  'private/mail-config', 'private/calendar-config',
                  'private/db-password')


def valid_staged_members(stage):
    for name in REQUIRED_DIRECTORIES:
        path = stage / name
        if not path.is_dir() or path.is_symlink():
            RECOVERY.fail('stage_members_invalid')
    for name in REQUIRED_FILES:
        path = stage / name
        if not path.is_file() or path.is_symlink():
            RECOVERY.fail('stage_members_invalid')
    if (stage / 'env').stat().st_mode & 0o077:
        RECOVERY.fail('stage_env_permissions_unsafe')
    if (stage / 'postgres.dump').stat().st_size == 0:
        RECOVERY.fail('stage_dump_empty')
    for root, directories, files in os.walk(stage, followlinks=False):
        for name in directories + files:
            item = Path(root) / name
            info = item.lstat()
            relative = item.relative_to(stage).as_posix()
            if stat.S_ISLNK(info.st_mode):
                target = PurePosixPath(os.readlink(item))
                if (not relative.startswith('state/data/') or
                        not target.is_relative_to(PurePosixPath('/app')) or
                        '..' in target.parts):
                    RECOVERY.fail('stage_link_unsafe')
            elif not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                RECOVERY.fail('stage_member_unsafe')
            elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                RECOVERY.fail('stage_hardlink_unsafe')


def checked_revision(project, state, stage):
    staged = json.loads((stage / 'state/release.json').read_text())
    active = json.loads((state / 'release.json').read_text())
    revision = staged.get('revision')
    if (not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision) or
            active.get('revision') != revision):
        RECOVERY.fail('release_revision_mismatch')
    tree = staged.get('tree')
    if not isinstance(tree, str) or not re.fullmatch(r'[0-9a-f]{40}', tree):
        RECOVERY.fail('release_tree_invalid')
    staged_marker = json.loads((stage / 'state/data/upgrade-state.json').read_text())
    active_marker = json.loads((state / 'data/upgrade-state.json').read_text())
    if staged_marker.get('commit') != revision or active_marker.get('commit') != revision:
        RECOVERY.fail('release_marker_mismatch')
    if tree != active.get('tree'):
        RECOVERY.fail('release_tree_mismatch')
    head = RECOVERY.command(['git', '-c', f'safe.directory={project}', 'rev-parse', 'HEAD'],
                            cwd=project).decode().strip()
    if head != revision:
        RECOVERY.fail('checkout_revision_mismatch')


def volume_identity(project, service, destination):
    RECOVERY.volume_mount(project, service, destination)
    container = RECOVERY.compose(project, 'ps', '-q', service).decode().strip()
    details = json.loads(RECOVERY.command(['docker', 'inspect', container]))[0]
    matches = [mount for mount in details['Mounts'] if mount['Destination'] == destination]
    if len(matches) != 1 or matches[0]['Type'] != 'volume':
        RECOVERY.fail('volume_mount_unexpected')
    return matches[0]['Name']


def preflight(args):
    project = RECOVERY.source_path(args.project_root, kind='dir')
    state = RECOVERY.source_path(args.state_root, kind='dir')
    stage = RECOVERY.private_directory(Path(args.stage_dir))
    RECOVERY.not_nested(project, state)
    RECOVERY.not_nested(project, stage)
    RECOVERY.not_nested(state, stage)
    if not (project / 'compose.yaml').is_file():
        RECOVERY.fail('compose_file_missing')
    valid_staged_members(stage)
    checked_revision(project, state, stage)
    active_env = RECOVERY.source_path(str(project / '.env'), kind='file')
    if active_env.stat().st_mode & 0o077:
        RECOVERY.fail('env_permissions_unsafe')
    if RECOVERY.sha256(active_env) != RECOVERY.sha256(stage / 'env'):
        RECOVERY.fail('environment_changed_since_backup')
    install, _ = RECOVERY.read_env_paths(active_env)
    with sqlite3.connect(f'file:{stage / "state/data/v2.db"}?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            RECOVERY.fail('stage_sqlite_invalid')
    with (stage / 'postgres.dump').open('rb') as dump:
        listing = subprocess.run(
            ['docker', 'compose', '-f', str(project / 'compose.yaml'), '--profile',
             RECOVERY.PROFILE, 'exec', '-T', 'postgres', 'pg_restore', '-l'],
            cwd=project, stdin=dump, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False)
    if listing.returncode or b'TABLE' not in listing.stdout:
        RECOVERY.fail('stage_postgres_dump_invalid')
    config = json.loads(RECOVERY.compose(project, 'config', '--format', 'json'))
    volumes = config.get('services', {}).get('nanoclaw', {}).get('volumes', [])
    binds = {item.get('target'): item.get('source') for item in volumes
             if item.get('type') == 'bind'}
    if (binds.get('/srv/nanoclaw/data') != str(state / 'data') or
            binds.get('/srv/nanoclaw/release.json') != str(state / 'release.json')):
        RECOVERY.fail('state_bind_mismatch')
    onecli_volume = volume_identity(project, 'onecli', '/app/data')
    postgres_volume = volume_identity(project, 'postgres', '/var/lib/postgresql')
    if onecli_volume == postgres_volume:
        RECOVERY.fail('volume_identity_conflict')
    RECOVERY.running_agents(install)
    print('cutover_preflight=ok', flush=True)
    print('cutover_mutation=disabled', flush=True)


def main():
    parser = RECOVERY.PrivateArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--stage-dir', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        RECOVERY.fail('root_required')
    preflight(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('cutover_preflight=failed', flush=True)
        code = str(error).split(':', 1)[0][:80]
        print('failure_category=' + code if isinstance(error, RECOVERY.RecoveryError)
              else 'failure_category=unexpected_error', flush=True)
        sys.exit(2)
