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
import time
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


def command(argv, *, cwd=None, owner=None, timeout=600, merge_stderr=False):
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
                            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                            timeout=timeout, preexec_fn=drop, check=False)
    require(result.returncode == 0, 'command_failed')
    return result.stdout.decode('utf-8').strip()


def git(project, *args, owner=None):
    return command(['git', '-C', str(project), *args], cwd=project, owner=owner,
                   timeout=60)


def compose(project, *args, timeout=600):
    return command(['docker', 'compose', '-f', str(project / 'compose.yaml'),
                    '--profile', RECOVERY.PROFILE, *args], cwd=project, timeout=timeout)


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


SCHEMA_PATHS = ('src/db/migrations', 'src/modules', 'src/mailbox/sqlite/schema.ts')
LOG_TITLE = re.compile(r'\s(?:INFO|WARN|ERROR)\s+(.+?)(?:\s+[A-Za-z_][A-Za-z0-9_]*=|$)')
ANSI = re.compile(r'\x1b\[[0-9;]*m')


def schema_fingerprint(project, revision, owner):
    """Blob ids of every file that defines the central or session DB schema at a revision."""
    listing = git(project, 'ls-tree', '-r', revision, '--', *SCHEMA_PATHS, owner=owner)
    entries = []
    for line in listing.splitlines():
        meta, path = line.split('\t', 1)
        if path.endswith('.test.ts'):
            continue
        if path.startswith('src/modules/') and '/migrations/' not in path:
            continue
        entries.append((path, meta.split()[2]))
    return sorted(entries)


def backup_age_minutes(manifest):
    created = datetime.strptime(manifest.get('created_utc', ''), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 60


def log_titles(text):
    """Message titles of host log lines (no key=value parameters)."""
    titles = []
    for line in ANSI.sub('', text).splitlines():
        match = LOG_TITLE.search(line)
        if match:
            titles.append(match.group(1).strip())
    return titles


def expected_channels(values):
    return 1 + bool(values.get('SIGNAL_ACCOUNT')) + bool(values.get('TELEGRAM_BOT_TOKEN'))


def channels_ready(titles, values):
    started = sum(1 for title in titles if title == 'Channel adapter started')
    signal_ok = not values.get('SIGNAL_ACCOUNT') or 'Signal channel connected' in titles
    return started >= expected_channels(values) and signal_ok


def wait_for_channels(project, values, since, timeout=120):
    container = compose(project, 'ps', '-q', 'nanoclaw')
    deadline = time.time() + timeout
    while time.time() < deadline:
        titles = log_titles(command(['docker', 'logs', '--since', since, container], timeout=60,
                                    merge_stderr=True))
        if channels_ready(titles, values):
            print(f'channels_started={sum(1 for t in titles if t == "Channel adapter started")}', flush=True)
            return
        time.sleep(5)
    fail('channels_not_ready')


def refresh_derived_images(project, state, target):
    """Rebuild every per-group image on the new base now, instead of on the first message."""
    with sqlite3.connect(f'file:{state / "data/v2.db"}?mode=ro', uri=True) as db:
        rows = db.execute("SELECT agent_group_id FROM container_configs WHERE image_tag IS NOT NULL AND "
                          "((packages_apt IS NOT NULL AND packages_apt NOT IN ('', '[]')) OR "
                          "(packages_npm IS NOT NULL AND packages_npm NOT IN ('', '[]')))").fetchall()
    base = command(['docker', 'image', 'inspect', '-f', '{{.Id}}', target['images']['agent']])
    for (group,) in rows:
        require(re.fullmatch(r'[A-Za-z0-9._-]{1,128}', group), 'agent_group_id_invalid')
        compose(project, 'exec', '-T', 'nanoclaw', 'node', 'dist/cli/client.js', 'groups', 'restart',
                '--id', group, '--rebuild', timeout=1200)
        with sqlite3.connect(f'file:{state / "data/v2.db"}?mode=ro', uri=True) as db:
            tag = db.execute('SELECT image_tag FROM container_configs WHERE agent_group_id = ?',
                             (group,)).fetchone()[0]
        labels = json.loads(command(['docker', 'image', 'inspect', '-f', '{{json .Config.Labels}}', tag])) or {}
        require(labels.get('dev.nanoclaw.derived-from') == base and
                labels.get('org.opencontainers.image.revision') == target['revision'],
                'derived_image_not_refreshed')
    print(f'derived_images_refreshed={len(rows)}', flush=True)


def stop_agents(install):
    agents = RECOVERY.running_agents(install)
    if agents:
        command(['docker', 'stop', *agents])
    print(f'agent_containers_stopped={len(agents)}', flush=True)


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
    production = bool(getattr(args, 'confirm_production', False))
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
            (production or current['version'] == target['version']) and
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
    if production:
        require(re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', values.get('NANOCLAW_INSTALL_ID', '')),
                'install_id_invalid')
    else:
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
    if production:
        # A production rollback restores code and control files only; a schema
        # change would need the data restored from the backup as well.
        require(schema_fingerprint(project, current['revision'], owner) ==
                schema_fingerprint(project, target['revision'], owner),
                'schema_change_requires_full_restore')
    else:
        no_agents(values['NANOCLAW_INSTALL_ID'])
    service_health(project)
    backup_manifest = json.loads((backup_dir / 'manifest.json').read_bytes())
    require(backup_manifest.get('revision') == current['revision'],
            'backup_release_mismatch')
    if production:
        try:
            age = backup_age_minutes(backup_manifest)
        except ValueError:
            fail('backup_age_unknown')
        require(0 <= age <= args.max_backup_age_minutes, 'backup_too_old')
    RECOVERY.verify_or_stage(argparse.Namespace(
        action='verify', backup_dir=str(backup_dir), key_file=str(key_file)))
    print('release_update_preflight=ok', flush=True)
    print(f'release_update_mode={"production" if production else "synthetic"}', flush=True)
    return (project, controls, current_bytes, metadata, current, target_blob, target, owner, values,
            state, production)


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
    (project, controls, current_bytes, metadata, current, target_blob, target, owner, values,
     state, production) = context
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
        if production:
            stop_agents(values['NANOCLAW_INSTALL_ID'])
        else:
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
        since = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        compose(project, 'up', '-d', '--wait', '--no-deps', '--no-build', '--pull',
                'never', '--force-recreate', 'nanoclaw')
        service_health(project)
        release_state(project, controls, target, owner)
        if production:
            wait_for_channels(project, values, since)
            refresh_derived_images(project, state, target)
            service_health(project)
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
                if production:
                    stop_agents(values['NANOCLAW_INSTALL_ID'])
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
    parser.add_argument('--confirm-production', action='store_true',
                        help='install with real identities: same schema only, fresh backup required')
    parser.add_argument('--max-backup-age-minutes', type=int, default=120)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    require(args.confirm_synthetic != args.confirm_production, 'mode_confirmation_required')
    require(0 < args.max_backup_age_minutes <= 24 * 60, 'backup_age_limit_invalid')
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
