#!/usr/bin/env python3
"""Guarded, synthetic-only update between digest-pinned Compose releases.

The operator must create and verify a full encrypted backup separately. This
transaction changes only the checkout, three control files and affected Compose
services. It never prints private values, command output or Docker logs.
"""

import argparse
import fcntl
import importlib.util
import json
import os
import pwd
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    'compose_recovery', Path(__file__).with_name('compose-recovery.py'))
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)

IMAGE_KEYS = {
    'host': 'NANOCLAW_HOST_IMAGE',
    'agent': 'NANOCLAW_AGENT_IMAGE',
    'brokers': 'NANOCLAW_BROKER_IMAGE',
    'onecli': 'ONECLI_IMAGE',
    'postgres': 'POSTGRES_IMAGE',
    'signal': 'SIGNAL_IMAGE',
}
FORK_IMAGES = ('host', 'agent', 'brokers')
SUPPORT = ('onecli-egress', 'infomaniak-mail', 'nextcloud-calendar')
SERVICES = ('nanoclaw', *SUPPORT, 'signal-cli', 'onecli', 'postgres')
COMMIT = re.compile(r'[0-9a-f]{40}\Z')
PINNED = re.compile(r'[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z')


class UpdateError(Exception):
    pass


def fail(code):
    raise UpdateError(code)


def require(condition, code):
    if not condition:
        fail(code)


def command(argv, *, cwd=None, owner=None, timeout=600):
    environment = os.environ.copy()
    drop = None
    if owner is not None:
        account = pwd.getpwuid(owner.st_uid)
        environment.update(HOME=account.pw_dir, USER=account.pw_name,
                           LOGNAME=account.pw_name)

        def drop():
            os.setgroups([])
            os.setgid(owner.st_gid)
            os.setuid(owner.st_uid)

    result = subprocess.run(argv, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout,
                            preexec_fn=drop, check=False)
    require(result.returncode == 0, 'command_failed')
    return result.stdout.decode('utf-8').strip()


def git(project, *args, owner=None):
    return command(['git', '-C', str(project), *args], cwd=project, owner=owner,
                   timeout=60)


def compose(project, *args):
    return command(['docker', 'compose', '-f', str(project / 'compose.yaml'),
                    '--profile', RECOVERY.PROFILE, *args], cwd=project)


def regular_private(path, owner):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            info.st_uid == owner.st_uid and stat.S_IMODE(info.st_mode) == 0o600,
            'control_file_unsafe')
    return path.read_bytes(), info


def read_env(content):
    values, counts = {}, {}
    for line in content.decode('utf-8').splitlines():
        if line.lstrip().startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key), 'environment_key_invalid')
        counts[key] = counts.get(key, 0) + 1
        values[key] = value.strip().strip('"\'')
    return values, counts


def rewrite_env(content, images):
    lines = content.decode('utf-8').splitlines(keepends=True)
    for name in FORK_IMAGES:
        key = IMAGE_KEYS[name]
        matches = [index for index, line in enumerate(lines) if line.startswith(key + '=')]
        require(len(matches) == 1, 'image_key_count_invalid')
        previous = lines[matches[0]]
        newline = '\r\n' if previous.endswith('\r\n') else '\n' if previous.endswith('\n') else ''
        lines[matches[0]] = f'{key}={images[name]}{newline}'
    return ''.join(lines).encode('utf-8')


def validate_manifest(blob):
    require(len(blob) <= 16 * 1024, 'release_manifest_invalid')
    manifest = json.loads(blob)
    require(isinstance(manifest, dict) and
            set(manifest) == {'schema', 'version', 'revision', 'tree', 'images'} and
            manifest['schema'] == 'nanoclaw-compose-release/v1' and
            isinstance(manifest['version'], str) and
            re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?', manifest['version']) and
            isinstance(manifest['revision'], str) and COMMIT.fullmatch(manifest['revision']) and
            isinstance(manifest['tree'], str) and COMMIT.fullmatch(manifest['tree']) and
            isinstance(manifest['images'], dict) and set(manifest['images']) == set(IMAGE_KEYS),
            'release_manifest_invalid')
    for ref in manifest['images'].values():
        require(isinstance(ref, str) and PINNED.fullmatch(ref) and
                not ref.startswith('example.invalid/'), 'release_image_unpinned')
    return manifest


def atomic_write(path, content, metadata):
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.release-', dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
        os.fchown(fd, metadata.st_uid, metadata.st_gid)
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def service_health(project):
    compose(project, 'config', '--quiet')
    for service in SERVICES:
        container = compose(project, 'ps', '-q', service)
        require(container, 'service_missing')
        info = json.loads(command(['docker', 'inspect', container]))[0]
        state = info.get('State', {})
        require(state.get('Running') is True, 'service_not_running')
        if service != 'nanoclaw':
            require(state.get('Health', {}).get('Status') == 'healthy',
                    'service_unhealthy')


def no_agents(install):
    require(not RECOVERY.running_agents(install), 'active_agent_container_present')


def require_synthetic(values, channels):
    require(not values.get('TELEGRAM_BOT_TOKEN') and
            not values.get('SIGNAL_ACCOUNT') and
            re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',
                         values.get('NANOCLAW_INSTALL_ID', '')) and
            channels == {'cli'}, 'synthetic_identity_required')


def release_state(project, controls, expected, owner):
    require(git(project, 'rev-parse', 'HEAD', owner=owner) == expected['revision'] and
            git(project, 'rev-parse', 'HEAD^{tree}', owner=owner) == expected['tree'] and
            not git(project, 'status', '--porcelain', owner=owner),
            'checkout_release_mismatch')
    active = validate_manifest(controls[1].read_bytes())
    marker = json.loads(controls[2].read_bytes())
    values, counts = read_env(controls[0].read_bytes())
    require(active == expected and marker.get('commit') == expected['revision'] and
            marker.get('tree') == expected['tree'] and
            marker.get('version') == expected['version'], 'control_state_mismatch')
    for name, key in IMAGE_KEYS.items():
        require(counts.get(key) == 1 and values.get(key) == expected['images'][name],
                'environment_image_mismatch')
    return values


def preflight(args):
    project = RECOVERY.source_path(args.project_root, kind='dir')
    state = RECOVERY.source_path(args.state_root, kind='dir')
    target_file = RECOVERY.source_path(args.release_manifest, kind='file')
    backup_root = RECOVERY.private_directory(Path(args.control_backup_root))
    backup_dir = RECOVERY.private_directory(Path(args.backup_dir))
    key_file = RECOVERY.source_path(args.key_file, kind='file')
    for path in (state, target_file, backup_root, backup_dir, key_file.parent):
        RECOVERY.not_nested(project, path)
    for path in (target_file, backup_root, backup_dir, key_file.parent):
        RECOVERY.not_nested(state, path)
    RECOVERY.not_nested(backup_root, backup_dir)
    RECOVERY.not_nested(backup_root, key_file.parent)
    require((project / 'compose.yaml').is_file(), 'compose_file_missing')
    owner = project.stat()
    require(owner.st_uid != 0, 'project_owner_invalid')
    controls = (project / '.env', state / 'release.json',
                state / 'data/upgrade-state.json')
    current_bytes = {}
    metadata = {}
    for path in controls:
        current_bytes[path], metadata[path] = regular_private(path, owner)
    current = validate_manifest(current_bytes[controls[1]])
    target_info = target_file.lstat()
    require(stat.S_ISREG(target_info.st_mode) and target_info.st_nlink == 1 and
            not target_info.st_mode & 0o022 and target_info.st_size <= 16 * 1024,
            'target_manifest_unsafe')
    target_blob = target_file.read_bytes()
    target = validate_manifest(target_blob)
    require(current['revision'] != target['revision'] and
            current['version'] == target['version'] and
            all(current['images'][name] == target['images'][name]
                for name in ('onecli', 'postgres', 'signal')),
            'unsupported_release_transition')
    values = release_state(project, controls, current, owner)
    _, env_counts = read_env(current_bytes[controls[0]])
    require(env_counts.get('TELEGRAM_BOT_TOKEN', 0) <= 1 and
            env_counts.get('SIGNAL_ACCOUNT', 0) <= 1,
            'synthetic_identity_required')
    with sqlite3.connect(f'file:{state / "data/v2.db"}?mode=ro', uri=True) as db:
        require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok',
                'synthetic_state_invalid')
        channels = {row[0] for row in db.execute(
            'SELECT DISTINCT channel_type FROM messaging_groups')}
    require_synthetic(values, channels)
    require(git(project, 'rev-parse', f'{target["revision"]}^{{commit}}', owner=owner) ==
            target['revision'] and
            git(project, 'rev-parse', f'{target["revision"]}^{{tree}}', owner=owner) ==
            target['tree'] and
            git(project, 'merge-base', current['revision'], target['revision'], owner=owner) ==
            current['revision'], 'target_checkout_mismatch')
    for name in FORK_IMAGES:
        info = json.loads(command(['docker', 'image', 'inspect',
                                   target['images'][name]]))[0]
        labels = info.get('Config', {}).get('Labels') or {}
        require(labels.get('org.opencontainers.image.revision') == target['revision'] and
                labels.get('org.olaclaw.source.tree') == target['tree'] and
                info.get('Os') == 'linux' and info.get('Architecture') == 'amd64',
                'target_image_identity_mismatch')
    no_agents(values['NANOCLAW_INSTALL_ID'])
    service_health(project)
    backup_manifest = json.loads((backup_dir / 'manifest.json').read_bytes())
    require(backup_manifest.get('revision') == current['revision'],
            'backup_release_mismatch')
    RECOVERY.verify_or_stage(argparse.Namespace(
        action='verify', backup_dir=str(backup_dir), key_file=str(key_file)))
    print('release_update_preflight=ok', flush=True)
    return project, controls, current_bytes, metadata, current, target_blob, target, owner, values


def control_backup(root, current_bytes, controls):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = root / f'control-{stamp}-{secrets.token_hex(4)}'
    folder.mkdir(mode=0o700)
    for name, path in zip(('env.old', 'release.old.json', 'marker.old.json'), controls):
        with (folder / name).open('xb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(current_bytes[path])
            output.flush()
            os.fsync(output.fileno())
    print('control_backup=created', flush=True)


def restore_controls(controls, contents, metadata):
    for path in controls:
        atomic_write(path, contents[path], metadata[path])


def controls_unchanged(controls, contents):
    require(all(path.read_bytes() == contents[path] for path in controls),
            'control_files_changed_since_preflight')


def apply_update(context, backup_root):
    project, controls, current_bytes, metadata, current, target_blob, target, owner, values = context
    controls_unchanged(controls, current_bytes)
    next_env = rewrite_env(current_bytes[controls[0]], target['images'])
    marker = json.loads(current_bytes[controls[2]])
    marker.update(version=target['version'], commit=target['revision'],
                  tree=target['tree'], updatedAt=datetime.now(timezone.utc).isoformat(),
                  via='compose-release-update')
    next_marker = (json.dumps(marker, indent=2) + '\n').encode('utf-8')
    control_backup(backup_root, current_bytes, controls)
    stop_attempted = False
    try:
        stop_attempted = True
        compose(project, 'stop', 'nanoclaw')
        print('old_host_stopped=yes', flush=True)
        no_agents(values['NANOCLAW_INSTALL_ID'])
        controls_unchanged(controls, current_bytes)
        git(project, 'switch', '--detach', target['revision'], owner=owner)
        require(git(project, 'rev-parse', 'HEAD', owner=owner) == target['revision'],
                'checkout_switch_failed')
        for path, content in zip(controls, (next_env, target_blob, next_marker)):
            atomic_write(path, content, metadata[path])
        release_state(project, controls, target, owner)
        compose(project, 'up', '-d', '--wait', '--no-deps', '--no-build', '--pull',
                'never', '--force-recreate', *SUPPORT)
        compose(project, 'up', '-d', '--wait', '--no-deps', '--no-build', '--pull',
                'never', '--force-recreate', 'nanoclaw')
        service_health(project)
        release_state(project, controls, target, owner)
        print('release_update=healthy', flush=True)
    except Exception as error:
        print('release_update=failed', flush=True)
        print('failure_category=' + (str(error) if isinstance(error, UpdateError)
                                     else 'unexpected_error'), flush=True)
        if not stop_attempted:
            print('rollback=not_needed', flush=True)
            raise UpdateError('update_failed') from error
        try:
            try:
                compose(project, 'stop', 'nanoclaw')
            except Exception:
                pass
            checkout_restored = True
            try:
                git(project, 'switch', '--detach', current['revision'], owner=owner)
            except Exception:
                checkout_restored = False
            restore_controls(controls, current_bytes, metadata)
            require(checkout_restored, 'checkout_rollback_failed')
            release_state(project, controls, current, owner)
            compose(project, 'up', '-d', '--wait', '--no-deps', '--no-build', '--pull',
                    'never', '--force-recreate', *SUPPORT)
            compose(project, 'up', '-d', '--wait', '--no-deps', '--no-build', '--pull',
                    'never', '--force-recreate', 'nanoclaw')
            service_health(project)
            print('rollback=healthy', flush=True)
        except Exception:
            print('rollback=failed_manual_recovery_needed', flush=True)
        raise UpdateError('update_failed') from error


def main():
    parser = RECOVERY.PrivateArgumentParser(description=__doc__)
    for flag in ('project-root', 'state-root', 'release-manifest', 'backup-dir',
                 'key-file', 'control-backup-root'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--confirm-synthetic', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    require(args.confirm_synthetic, 'synthetic_confirmation_required')
    backup_root = RECOVERY.private_directory(Path(args.control_backup_root))
    lock_path = backup_root / '.compose-release-update.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        context = preflight(args)
        if args.apply:
            apply_update(context, backup_root)
        else:
            print('release_update_mutation=disabled', flush=True)
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        if not isinstance(error, UpdateError) or str(error) != 'update_failed':
            print('release_update=failed', flush=True)
            print('failure_category=' + (str(error) if isinstance(error, (UpdateError, RECOVERY.RecoveryError))
                                         else 'unexpected_error'), flush=True)
        sys.exit(2)
