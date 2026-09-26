#!/usr/bin/env python3
"""Private, operator-run backup and offline restore staging for Compose installs.

This tool never cuts over a live install. It emits fixed status labels only.
Keep the encryption key on separate storage from the backup archive.
"""

import argparse
import hashlib
import hmac
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


SCHEMA = 'nanoclaw-compose-backup/v1'
PROFILE = 'core-preview'
STOP_SERVICES = ('nanoclaw', 'signal-cli', 'infomaniak-mail',
                 'nextcloud-calendar', 'onecli-egress', 'onecli')
PRIVATE_INPUTS = {
    'INFOMANIAK_BROKER_CONFIG_FILE': 'private/mail-config',
    'NEXTCLOUD_BROKER_CONFIG_FILE': 'private/calendar-config',
    'ONECLI_DB_PASSWORD_FILE': 'private/db-password',
    'INFOMANIAK_DOWNLOAD_DIR': 'private/mail-downloads',
}
REQUIRED_MEMBERS = {'state', 'state/release.json', 'state/data/v2.db',
                    'state/data/upgrade-state.json', 'env', 'onecli-data',
                    *PRIVATE_INPUTS.values()}


class RecoveryError(Exception):
    pass


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        fail('invalid_arguments')


def fail(code):
    raise RecoveryError(code)


def command(argv, *, cwd=None, stdin=None, stdout=None):
    result = subprocess.run(argv, cwd=cwd, stdin=stdin, stdout=stdout or subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        fail('command_failed')
    return result.stdout if stdout is None else b''


def compose(project, *args):
    return command(['docker', 'compose', '-f', str(project / 'compose.yaml'),
                    '--profile', PROFILE, *args], cwd=project)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def key_bytes(path):
    value = path.read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{64}', value):
        fail('key_format_invalid')
    return bytes.fromhex(value)


def manifest_mac(payload, key):
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(key_bytes(key), canonical, hashlib.sha256).hexdigest()


def private_directory(path):
    if not path.is_absolute() or path == Path('/') or path.is_symlink():
        fail('path_symlink_refused')
    if not path.is_dir() or path.stat().st_mode & 0o077:
        fail('private_directory_required')
    if os.geteuid() == 0 and path.stat().st_uid != 0:
        fail('private_directory_owner_invalid')
    resolved = path.resolve(strict=True)
    if resolved == Path('/'):
        fail('path_symlink_refused')
    return resolved


def source_path(raw, *, kind):
    path = Path(raw)
    if not path.is_absolute() or path == Path('/') or path.is_symlink():
        fail('source_path_invalid')
    if kind == 'dir' and not path.is_dir():
        fail('source_directory_missing')
    if kind == 'file' and not path.is_file():
        fail('source_file_missing')
    resolved = path.resolve(strict=True)
    if resolved == Path('/'):
        fail('source_path_invalid')
    return resolved


def not_nested(path, other):
    if path == other or path.is_relative_to(other) or other.is_relative_to(path):
        fail('recovery_path_overlap')


def read_env_paths(env_file):
    values = {}
    for line in env_file.read_text().splitlines():
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key in PRIVATE_INPUTS or key == 'NANOCLAW_INSTALL_ID':
            values[key] = value.strip().strip('"\'')
    if set(PRIVATE_INPUTS) - values.keys():
        fail('private_input_missing')
    install = values.get('NANOCLAW_INSTALL_ID', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', install):
        fail('install_id_invalid')
    paths = {}
    for key, member in PRIVATE_INPUTS.items():
        paths[member] = source_path(values[key], kind='dir' if key.endswith('_DIR') else 'file')
    return install, paths


def expected_agent_name(install, session):
    raw = re.sub(r'[^a-zA-Z0-9_.-]', '-', f'{install}-{session}')
    if len(raw) <= 48:
        return f'ncl-{raw}'
    suffix = hashlib.sha256(f'{install}\0{session}'.encode()).hexdigest()[:8]
    return f'ncl-{raw[:39]}-{suffix}'


def running_agents(install):
    ids = command(['docker', 'ps', '-q', '--filter', f'label=nanoclaw-install={install}',
                   '--filter', 'label=nanoclaw-role=agent']).decode().splitlines()
    for container in ids:
        details = json.loads(command(['docker', 'inspect', container]))[0]
        labels = details['Config']['Labels']
        session = labels.get('nanoclaw-session', '')
        group = labels.get('nanoclaw-group', '')
        if (labels.get('nanoclaw-install') != install or
                labels.get('nanoclaw-role') != 'agent' or
                not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', session) or
                not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', group) or
                details['Name'] != '/' + expected_agent_name(install, session)):
            fail('agent_identity_unexpected')
    return ids


def volume_mount(project, service, destination):
    container = compose(project, 'ps', '-q', service).decode().strip()
    if not container:
        fail('service_missing')
    details = json.loads(command(['docker', 'inspect', container]))[0]
    matches = [mount for mount in details['Mounts'] if mount['Destination'] == destination]
    if len(matches) != 1 or matches[0]['Type'] != 'volume':
        fail('volume_mount_unexpected')
    volume = matches[0]['Name']
    mount = source_path(matches[0]['Source'], kind='dir')
    expected = source_path(command(['docker', 'volume', 'inspect', '-f',
                                    '{{.Mountpoint}}', volume]).decode().strip(), kind='dir')
    if mount != expected:
        fail('volume_mount_mismatch')
    return mount


def running_profile_services(project):
    return tuple(service for service in (*STOP_SERVICES, 'postgres')
                 if compose(project, 'ps', '-q', service).decode().strip())


def checked_members(archive):
    found = set()
    kinds = {}
    links = set()
    with tarfile.open(archive, 'r:') as stream:
        for item in stream:
            path = PurePosixPath(item.name)
            if (item.name.startswith('/') or path.is_absolute() or not path.parts or
                    '..' in path.parts or str(path) != item.name.rstrip('/') or
                    str(path) in found):
                fail('archive_member_unsafe')
            if any(parent in links for parent in (str(p) for p in path.parents)):
                fail('archive_member_unsafe')
            if item.issym():
                target = PurePosixPath(item.linkname)
                if (not str(path).startswith('state/data/') or
                        not target.is_relative_to(PurePosixPath('/app')) or
                        '..' in target.parts):
                    fail('archive_member_unsafe')
                links.add(str(path))
                kinds[str(path)] = 'link'
            elif item.isfile():
                kinds[str(path)] = 'file'
            elif item.isdir():
                kinds[str(path)] = 'dir'
            else:
                fail('archive_member_unsafe')
            found.add(str(path))
    if not REQUIRED_MEMBERS.issubset(found):
        fail('archive_members_missing')
    if (kinds.get('state') != 'dir' or kinds.get('onecli-data') != 'dir' or
            kinds.get('private/mail-downloads') != 'dir' or
            any(kinds.get(name) != 'file' for name in
                ('env', 'state/release.json', 'state/data/v2.db',
                 'state/data/upgrade-state.json', 'private/mail-config',
                 'private/calendar-config', 'private/db-password'))):
        fail('archive_member_unsafe')


def archive_sources(archive, state, env_file, onecli_data, extras):
    sources = [('state', state), ('env', env_file), ('onecli-data', onecli_data),
               *[(name, extras[name]) for name in PRIVATE_INPUTS.values()]]

    def archive_filter(item):
        if item.islnk():
            fail('source_hardlink_unsupported')
        return item if item.isfile() or item.isdir() or item.issym() else None

    with tarfile.open(archive, 'w:', format=tarfile.PAX_FORMAT) as stream:
        for name, path in sources:
            stream.add(path, arcname=name, recursive=True, filter=archive_filter)
    checked_members(archive)


def preserve_numeric_metadata(item, destination):
    # checked_members excludes hard links and defers the permitted symlinks.
    # Keep tar_filter's safe path and mode, while retaining numeric uid/gid:
    # data_filter clears ownership needed by UID 1000 services.
    filtered = tarfile.tar_filter(item, destination)
    return filtered.replace(uname='', gname='', deep=False)


def extract_checked(archive, target):
    checked_members(archive)
    with tarfile.open(archive, 'r:') as stream:
        members = stream.getmembers()
        symlinks = [item for item in members if item.issym()]
        stream.extractall(target, members=(item for item in members if not item.issym()),
                          filter=preserve_numeric_metadata)
        for item in symlinks:
            destination = target / item.name
            if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
                fail('archive_member_unsafe')
            if not destination.parent.resolve(strict=True).is_relative_to(target.resolve(strict=True)):
                fail('archive_member_unsafe')
            os.symlink(item.linkname, destination)


def encrypt(source, destination, key):
    plain_digest = sha256(source)
    command(['openssl', 'enc', '-aes-256-cbc', '-pbkdf2', '-iter', '200000',
             '-salt', '-in', str(source), '-out', str(destination),
             '-pass', f'file:{key}'])
    return {'encrypted_sha256': sha256(destination),
            'plain_sha256': plain_digest, 'bytes': destination.stat().st_size}


def decrypt(source, destination, key, checks):
    if sha256(source) != checks['encrypted_sha256']:
        fail('encrypted_hash_mismatch')
    command(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '200000',
             '-in', str(source), '-out', str(destination), '-pass', f'file:{key}'])
    if sha256(destination) != checks['plain_sha256']:
        fail('plain_hash_mismatch')


def backup(args):
    project = source_path(args.project_root, kind='dir')
    state = source_path(args.state_root, kind='dir')
    not_nested(project, state)
    backup_root = private_directory(Path(args.backup_root))
    key_root = private_directory(Path(args.key_root))
    for root in (backup_root, key_root):
        not_nested(root, project)
        not_nested(root, state)
    not_nested(backup_root, key_root)
    env_file = source_path(str(project / '.env'), kind='file')
    if env_file.stat().st_mode & 0o077:
        fail('env_permissions_unsafe')
    install, extras = read_env_paths(env_file)
    for path in extras.values():
        if path.is_relative_to(project):
            fail('private_input_inside_project')
        if path.is_file() and path.stat().st_mode & 0o077:
            fail('private_input_permissions_unsafe')
    manifest = json.loads((state / 'release.json').read_text())
    revision = manifest.get('revision')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        fail('release_revision_invalid')
    marker = json.loads((state / 'data/upgrade-state.json').read_text())
    if marker.get('commit') != revision:
        fail('release_marker_mismatch')
    if command(['git', '-c', f'safe.directory={project}', 'rev-parse', 'HEAD'],
               cwd=project).decode().strip() != revision:
        fail('checkout_revision_mismatch')
    with sqlite3.connect(f'file:{state / "data/v2.db"}?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            fail('state_sqlite_invalid')
    compose(project, 'config', '--quiet')
    onecli_data = volume_mount(project, 'onecli', '/app/data')
    volume_mount(project, 'postgres', '/var/lib/postgresql')
    running_agents(install)
    running_services = running_profile_services(project)
    if 'postgres' not in running_services:
        fail('postgres_not_running')
    print('backup_preflight=ok', flush=True)
    if not args.apply:
        return

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    identifier = f'{revision[:8]}-{stamp}-{secrets.token_hex(3)}'
    folder = backup_root / identifier
    key = key_root / f'{identifier}.key'
    folder.mkdir(mode=0o700)
    with key.open('x') as stream:
        stream.write(secrets.token_hex(32) + '\n')
    os.chmod(key, 0o600)
    stopped = False
    dump = folder / 'postgres.dump'
    archive = folder / 'state.tar'
    try:
        stopped = True
        compose(project, 'stop', 'nanoclaw')
        agents = running_agents(install)
        if agents:
            command(['docker', 'stop', *agents])
        compose(project, 'stop', *STOP_SERVICES[1:])
        print('backup_writers_stopped=yes', flush=True)

        with dump.open('wb') as stream:
            command(['docker', 'compose', '-f', str(project / 'compose.yaml'),
                     '--profile', PROFILE, 'exec', '-T', 'postgres',
                     'pg_dump', '-U', 'onecli', '-d', 'onecli', '-Fc'],
                    cwd=project, stdout=stream)
        if dump.stat().st_size == 0:
            fail('postgres_dump_empty')
        with dump.open('rb') as stream:
            listing = subprocess.run(['docker', 'compose', '-f', str(project / 'compose.yaml'),
                                      '--profile', PROFILE, 'exec', '-T', 'postgres',
                                      'pg_restore', '-l'], cwd=project, stdin=stream,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if listing.returncode or b'TABLE' not in listing.stdout:
            fail('postgres_dump_invalid')
        compose(project, 'stop', 'postgres')

        archive_sources(archive, state, env_file, onecli_data, extras)
        archive_checks = encrypt(archive, folder / 'state.tar.enc', key)
        dump_checks = encrypt(dump, folder / 'postgres.dump.enc', key)
        archive.unlink()
        dump.unlink()
        payload = {
            'schema': SCHEMA, 'revision': revision, 'created_utc': stamp,
            'state': archive_checks, 'postgres': dump_checks,
        }
        payload['hmac_sha256'] = manifest_mac(payload, key)
        (folder / 'manifest.json').write_text(json.dumps(payload, sort_keys=True, indent=2) + '\n')
        os.chmod(folder / 'manifest.json', 0o600)
        print('backup=verified', flush=True)
    finally:
        archive.unlink(missing_ok=True)
        dump.unlink(missing_ok=True)
        if stopped:
            try:
                compose(project, 'start', '--wait', *running_services)
                print('original_stack=healthy', flush=True)
            except Exception:
                print('original_stack=restart_failed', flush=True)
                raise


def verify_or_stage(args):
    folder = private_directory(Path(args.backup_dir))
    key = source_path(args.key_file, kind='file')
    not_nested(folder, key.parent)
    if key.stat().st_mode & 0o077:
        fail('key_permissions_unsafe')
    if os.geteuid() == 0 and key.stat().st_uid != 0:
        fail('key_owner_invalid')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest.get('schema') != SCHEMA:
        fail('backup_schema_invalid')
    mac = manifest.pop('hmac_sha256', None)
    if not isinstance(mac, str) or not hmac.compare_digest(mac, manifest_mac(manifest, key)):
        fail('backup_authentication_failed')
    target = None
    if args.action == 'stage':
        if not args.confirm_sensitive_plaintext:
            fail('plaintext_confirmation_required')
        target = Path(args.target_dir)
        if target.exists() or target.is_symlink() or not target.is_absolute():
            fail('stage_target_must_be_absent_absolute')
        target = target.resolve(strict=False)
        private_directory(target.parent)
        not_nested(target, folder)
        not_nested(target, key.parent)
    with tempfile.TemporaryDirectory(prefix='.recovery-verify-', dir=folder) as temp:
        work = Path(temp)
        os.chmod(work, 0o700)
        archive = work / 'state.tar'
        dump = work / 'postgres.dump'
        decrypt(folder / 'state.tar.enc', archive, key, manifest['state'])
        decrypt(folder / 'postgres.dump.enc', dump, key, manifest['postgres'])
        checked_members(archive)
        with tarfile.open(archive, 'r:') as stream:
            release = stream.extractfile('state/release.json')
            if release is None or json.load(release).get('revision') != manifest['revision']:
                fail('backup_revision_mismatch')
            marker = stream.extractfile('state/data/upgrade-state.json')
            if marker is None or json.load(marker).get('commit') != manifest['revision']:
                fail('backup_marker_mismatch')
            database = stream.extractfile('state/data/v2.db')
            if database is None:
                fail('backup_sqlite_missing')
            sqlite_copy = work / 'v2.db'
            with sqlite_copy.open('wb') as output:
                shutil.copyfileobj(database, output)
            for suffix in ('-wal', '-shm'):
                try:
                    sidecar = stream.extractfile('state/data/v2.db' + suffix)
                except KeyError:
                    sidecar = None
                if sidecar is not None:
                    with (work / ('v2.db' + suffix)).open('wb') as output:
                        shutil.copyfileobj(sidecar, output)
        with sqlite3.connect(f'file:{sqlite_copy}?mode=ro', uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                fail('backup_sqlite_invalid')
        print('backup_verify=ok', flush=True)
        if target is None:
            return
        target.mkdir(mode=0o700, parents=False)
        try:
            extract_checked(archive, target)
            shutil.copy2(dump, target / 'postgres.dump')
            os.chmod(target / 'postgres.dump', 0o600)
            with sqlite3.connect(f'file:{target / "state/data/v2.db"}?mode=ro', uri=True) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    fail('staged_sqlite_invalid')
        except Exception:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            raise
        print('restore_stage=ok', flush=True)


def main():
    parser = PrivateArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    backup_parser = sub.add_parser('backup', help='preflight; --apply creates encrypted backup')
    backup_parser.add_argument('--project-root', required=True)
    backup_parser.add_argument('--state-root', required=True)
    backup_parser.add_argument('--backup-root', required=True)
    backup_parser.add_argument('--key-root', required=True)
    backup_parser.add_argument('--apply', action='store_true')
    for name in ('verify', 'stage'):
        entry = sub.add_parser(name, help='offline verification or sensitive restore staging')
        entry.add_argument('--backup-dir', required=True)
        entry.add_argument('--key-file', required=True)
        if name == 'stage':
            entry.add_argument('--target-dir', required=True)
            entry.add_argument('--confirm-sensitive-plaintext', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        fail('root_required')
    if args.action == 'backup':
        backup(args)
    else:
        verify_or_stage(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('recovery_command=failed', flush=True)
        code = str(error).split(':', 1)[0][:80]
        print('failure_category=' + code if isinstance(error, RecoveryError) else 'failure_category=unexpected_error', flush=True)
        sys.exit(2)
