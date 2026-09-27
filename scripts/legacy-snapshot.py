#!/usr/bin/env python3
"""Consistent, encrypted snapshot of a checkout-based NanoClaw install.

Operator-run, as root, on the machine of the install being migrated. Without
`--apply` it is a read-only preflight. With `--apply` it stops the NanoClaw
host (which also stops a host-managed Signal daemon) and the mail broker,
stops OneCLI while PostgreSQL keeps running for a custom-format dump, archives
the install state, restarts everything, and only then encrypts the archive.

Only fixed status labels are printed: never paths, identifiers, environment
values, message content or command output. The encryption key is written to a
separate owner-only directory; the archive is useless without it.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    'compose_recovery', Path(__file__).with_name('compose-recovery.py'))
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)

SCHEMA = 'nanoclaw-legacy-snapshot/v1'
MAIL_UNIT = 'nanoclaw-infomaniak-mail.service'
COUNT_TABLES = ('agent_groups', 'messaging_groups', 'messaging_group_agents', 'users',
                'user_roles', 'sessions', 'container_configs')
INSTALL_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}\Z')


class SnapshotError(Exception):
    pass


def fail(code):
    raise SnapshotError(code)


def require(condition, code):
    if not condition:
        fail(code)


def run(argv, *, user=None, check=True, timeout=600, stdout=None, cwd=None):
    env = dict(os.environ)
    if user is not None:
        env['XDG_RUNTIME_DIR'] = f'/run/user/{user.pw_uid}'
        argv = ['runuser', '-u', user.pw_name, '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{user.pw_uid}', *argv]
    result = subprocess.run(argv, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, timeout=timeout, env=env, cwd=cwd, check=False)
    if check and result.returncode:
        fail('command_failed')
    return result.returncode, (result.stdout.decode('utf-8', 'replace').strip() if stdout is None else '')


def install_slug(project, env):
    override = env.get('NANOCLAW_INSTALL_ID', '')
    if override:
        require(INSTALL_ID.fullmatch(override), 'install_id_invalid')
        return override
    return hashlib.sha1(str(project).encode()).hexdigest()[:8]


def read_env(path):
    values = {}
    for line in path.read_text(errors='replace').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"\'')
    return values


def unit_active(user, unit):
    code, _ = run(['systemctl', '--user', 'is-active', '--quiet', unit], user=user, check=False)
    return code == 0


def mail_config_path(user):
    code, text = run(['systemctl', '--user', 'cat', MAIL_UNIT], user=user, check=False)
    if code:
        return None
    line = next((l for l in text.splitlines() if l.startswith('ExecStart=')), '')
    parts = shlex.split(line.split('=', 1)[-1])
    if '--config' not in parts or parts.index('--config') + 1 >= len(parts):
        fail('mail_config_argument_missing')
    return Path(parts[parts.index('--config') + 1])


def docker_json(*args):
    _, out = run(['docker', *args])
    return json.loads(out)


def service_containers():
    """The running OneCLI and PostgreSQL containers, identified by image."""
    _, out = run(['docker', 'ps', '--format', '{{.ID}}\t{{.Image}}\t{{.Names}}'])
    onecli, postgres = [], []
    for line in out.splitlines():
        cid, image, name = (line.split('\t') + ['', ''])[:3]
        repo = image.split('@')[0].rsplit(':', 1)[0].rsplit('/', 1)[-1]
        if repo == 'onecli' and 'egress' not in name:
            onecli.append(cid)
        elif repo == 'postgres':
            postgres.append(cid)
    require(len(onecli) == 1 and len(postgres) == 1, 'onecli_postgres_containers_ambiguous')
    return onecli[0], postgres[0]


def onecli_data_source(onecli):
    info = docker_json('inspect', onecli)[0]
    mounts = [m for m in info.get('Mounts', []) if m.get('Destination') == '/app/data']
    require(len(mounts) == 1, 'onecli_data_mount_unexpected')
    source = Path(mounts[0]['Source'])
    require(source.is_dir() and not source.is_symlink(), 'onecli_data_source_invalid')
    return source


def postgres_identity(postgres):
    env = dict(e.split('=', 1) for e in docker_json('inspect', postgres)[0]['Config'].get('Env', []) if '=' in e)
    user = env.get('POSTGRES_USER', 'postgres')
    database = env.get('POSTGRES_DB', user)
    require(re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', user) and re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', database),
            'postgres_identity_invalid')
    return user, database


def db_counts(db_path):
    with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=10) as db:
        require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'central_db_invalid')
        counts = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in COUNT_TABLES}
        migrations = sorted(r[0] for r in db.execute('SELECT name FROM schema_version'))
    return counts, migrations


def agent_containers(slug):
    _, out = run(['docker', 'ps', '-q', '--filter', f'label=nanoclaw-install={slug}'])
    return out.split()


def inputs(args):
    project = RECOVERY.source_path(args.project_root, kind='dir')
    output_root = RECOVERY.private_directory(Path(args.output_root))
    key_root = RECOVERY.private_directory(Path(args.key_root))
    RECOVERY.not_nested(output_root, key_root)
    for root in (output_root, key_root):
        RECOVERY.not_nested(root, project)
    try:
        user = pwd.getpwnam(args.service_user)
    except KeyError:
        fail('service_user_missing')
    require(project.stat().st_uid == user.pw_uid, 'project_owner_mismatch')
    for name in ('data', 'groups'):
        require((project / name).is_dir() and not (project / name).is_symlink(), 'project_state_missing')
    env_file = project / '.env'
    require(env_file.is_file() and not env_file.is_symlink(), 'env_missing')
    require(not env_file.stat().st_mode & 0o077, 'env_permissions_unsafe')
    env = read_env(env_file)
    slug = install_slug(project, env)
    host_unit = f'nanoclaw-v2-{slug}.service'
    signal = Path(env.get('SIGNAL_DATA_DIR') or Path(user.pw_dir) / '.local/share/signal-cli')
    config_dir = Path(user.pw_dir) / '.config/nanoclaw'
    mail_config = mail_config_path(user)
    return dict(project=project, output_root=output_root, key_root=key_root, user=user, env_file=env_file,
                env=env, slug=slug, host_unit=host_unit, signal=signal, config_dir=config_dir,
                mail_config=mail_config)


def preflight(ctx):
    project, user = ctx['project'], ctx['user']
    _, head = run(['git', '-C', str(project), 'rev-parse', 'HEAD'], user=user)
    _, dirty = run(['git', '-C', str(project), 'status', '--porcelain', '--untracked-files=no'], user=user)
    require(re.fullmatch(r'[0-9a-f]{40}', head), 'checkout_head_invalid')
    require(not dirty, 'checkout_dirty')
    counts, migrations = db_counts(project / 'data/v2.db')
    require(unit_active(user, ctx['host_unit']), 'host_unit_not_active')
    mail = ctx['mail_config']
    if mail is not None:
        require(mail.is_file() and not mail.is_symlink() and not mail.stat().st_mode & 0o077, 'mail_config_unsafe')
        json.loads(mail.read_text())
    require(ctx['signal'].is_dir() and not ctx['signal'].is_symlink(), 'signal_state_missing')
    onecli, postgres = service_containers()
    onecli_data = onecli_data_source(onecli)
    pg_user, pg_db = postgres_identity(postgres)
    code, _ = run(['docker', 'exec', postgres, 'pg_isready', '-U', pg_user, '-d', pg_db], check=False)
    require(code == 0, 'postgres_not_ready')
    need = sum(p.stat().st_size for root in (project / 'data', project / 'groups', ctx['signal'])
               for p in root.rglob('*') if p.is_file() and not p.is_symlink())
    free = shutil.disk_usage(ctx['output_root']).free
    require(free > 2 * need + (1 << 30), 'output_space_insufficient')
    print('snapshot_preflight=ok', flush=True)
    print(f'mail_broker={"present" if mail else "absent"}', flush=True)
    return dict(head=head, counts=counts, migrations=migrations, onecli=onecli, postgres=postgres,
                onecli_data=onecli_data, pg_user=pg_user, pg_db=pg_db)


def archive_filter(item):
    # Sockets, FIFOs and devices are recreated by the services. Hard links
    # (e.g. package-manager installs in a group workspace) are kept: tarfile
    # stores one only when its inode is already in this archive.
    return item if item.isfile() or item.isdir() or item.issym() or item.islnk() else None


def write_archive(path, ctx, live):
    project = ctx['project']
    sources = [('data', project / 'data'), ('groups', project / 'groups'), ('env', ctx['env_file']),
               ('signal', ctx['signal']), ('onecli-data', live['onecli_data'])]
    if ctx['config_dir'].is_dir():
        sources.append(('config-nanoclaw', ctx['config_dir']))
    if ctx['mail_config'] is not None:
        sources.append(('mail-config', ctx['mail_config']))
    with tarfile.open(path, 'w:', format=tarfile.PAX_FORMAT) as stream:
        for name, source in sources:
            stream.add(source, arcname=name, recursive=True, filter=archive_filter)
    return [name for name, _ in sources]


def wait_until(predicate, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(2)
    return False


def snapshot(ctx, live):
    """Run the snapshot; on any failure remove the partial folder and its key (no plaintext left behind)."""
    created = []
    try:
        _snapshot(ctx, live, created)
    except BaseException:
        for path in created:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        print('partial_snapshot=removed', flush=True)
        raise


def unit_enabled(user, unit):
    code, _ = run(['systemctl', '--user', 'is-enabled', '--quiet', unit], user=user, check=False)
    return code == 0


def restart_policy(container):
    _, policy = run(['docker', 'inspect', '-f', '{{.HostConfig.RestartPolicy.Name}}', container])
    require(re.fullmatch(r'[a-z-]*', policy), 'restart_policy_unexpected')
    return policy or 'no'


def leave_stopped(ctx, live, folder):
    """Final snapshot: keep the old install down, also across reboots, and write its rollback."""
    user = ctx['user']
    units = [ctx['host_unit']] + ([MAIL_UNIT] if ctx['mail_config'] is not None else [])
    enabled = [unit for unit in units if unit_enabled(user, unit)]
    policies = {cid: restart_policy(cid) for cid in (live['postgres'], live['onecli'])}
    run(['docker', 'stop', live['postgres']])
    for cid in policies:
        run(['docker', 'update', '--restart=no', cid])
    for unit in enabled:
        run(['systemctl', '--user', 'disable', unit], user=user)
    env = f'XDG_RUNTIME_DIR=/run/user/{user.pw_uid}'
    lines = ['#!/bin/sh', '# Restore the old install after an unsuccessful cutover. Run as root on this machine,',
             '# and only after the new install has been stopped.', 'set -eu']
    lines += [f'docker update --restart={policy} {cid}' for cid, policy in policies.items()]
    lines += [f'docker start {live["postgres"]}', f'docker start {live["onecli"]}']
    lines += [f'runuser -u {user.pw_name} -- env {env} systemctl --user enable {unit}' for unit in enabled]
    if ctx['mail_config'] is not None:
        lines.append(f'runuser -u {user.pw_name} -- env {env} systemctl --user start {MAIL_UNIT}')
    lines.append(f'runuser -u {user.pw_name} -- env {env} systemctl --user start {ctx["host_unit"]}')
    # Beside the snapshot folder, not inside it: a later failure removes the partial
    # folder, and the rollback must survive exactly that case.
    script = folder.parent / f'{folder.name}.rollback-old-install.sh'
    script.write_text('\n'.join(lines) + '\n')
    os.chmod(script, 0o700)
    print('original_install=stopped_and_disabled', flush=True)
    print(f'units_disabled={len(enabled)} containers_restart_policy=no', flush=True)
    print(f'rollback_script={script.name}', flush=True)


def _snapshot(ctx, live, created):
    project, user = ctx['project'], ctx['user']
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    identifier = f'legacy-{live["head"][:8]}-{stamp}-{secrets.token_hex(3)}'
    folder = ctx['output_root'] / identifier
    key = ctx['key_root'] / f'{identifier}.key'
    folder.mkdir(mode=0o700)
    created.append(folder)
    created.append(key)
    with key.open('x') as stream:
        stream.write(secrets.token_hex(32) + '\n')
    os.chmod(key, 0o600)
    archive, dump = folder / 'state.tar', folder / 'postgres.dump'
    stopped = {'host': False, 'mail': False, 'onecli': False}
    restart_ok = False
    archived = False
    try:
        run(['systemctl', '--user', 'stop', ctx['host_unit']], user=user)
        stopped['host'] = True
        require(wait_until(lambda: not unit_active(user, ctx['host_unit'])), 'host_stop_timeout')
        agents = agent_containers(ctx['slug'])
        if agents:
            run(['docker', 'stop', *agents])
        if ctx['mail_config'] is not None:
            run(['systemctl', '--user', 'stop', MAIL_UNIT], user=user)
            stopped['mail'] = True
        run(['docker', 'stop', live['onecli']])
        stopped['onecli'] = True
        print('writers_stopped=yes', flush=True)
        # State must not have changed shape while the host was stopping.
        counts, migrations = db_counts(project / 'data/v2.db')
        require(migrations == live['migrations'], 'migrations_changed_during_snapshot')
        with dump.open('wb') as stream:
            run(['docker', 'exec', live['postgres'], 'pg_dump', '-U', live['pg_user'], '-d', live['pg_db'], '-Fc'],
                stdout=stream)
        require(dump.stat().st_size > 0, 'postgres_dump_empty')
        with dump.open('rb') as stream:
            listing = subprocess.run(['docker', 'exec', '-i', live['postgres'], 'pg_restore', '-l'], stdin=stream,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        require(listing.returncode == 0 and b'TABLE' in listing.stdout, 'postgres_dump_invalid')
        members = write_archive(archive, ctx, live)
        archived = True
        print('state_archived=yes', flush=True)
    finally:
        if ctx.get('final') and archived:
            # The cutover continues on the new host: the old install must stay down.
            try:
                leave_stopped(ctx, live, folder)
                restart_ok = True
            except Exception:
                print('original_install=final_stop_incomplete_manual_check_needed', flush=True)
        else:
            try:
                if stopped['onecli']:
                    run(['docker', 'start', live['onecli']])
                if stopped['mail']:
                    run(['systemctl', '--user', 'start', MAIL_UNIT], user=user)
                if stopped['host']:
                    run(['systemctl', '--user', 'start', ctx['host_unit']], user=user)
                    require(wait_until(lambda: unit_active(user, ctx['host_unit'])), 'host_restart_timeout')
                restart_ok = True
                print('original_install=restarted', flush=True)
            except Exception:
                # Keep the original failure, if any; restart_ok stays False below.
                print('original_install=restart_failed_manual_check_needed', flush=True)
    if not restart_ok:
        fail('original_restart_failed')
    try:
        state_checks = RECOVERY.encrypt(archive, folder / 'state.tar.enc', key)
        dump_checks = RECOVERY.encrypt(dump, folder / 'postgres.dump.enc', key)
    finally:
        archive.unlink(missing_ok=True)
        dump.unlink(missing_ok=True)
    payload = {
        'schema': SCHEMA, 'created_utc': stamp, 'revision': live['head'], 'members': members,
        'counts': counts, 'migrations': live['migrations'], 'legacy_project_root': str(project),
        'legacy_uid': ctx['user'].pw_uid, 'legacy_gid': ctx['user'].pw_gid,
        'postgres': {'user': live['pg_user'], 'database': live['pg_db'], **dump_checks},
        'state': state_checks, 'final': bool(ctx.get('final')),
    }
    payload['hmac_sha256'] = RECOVERY.manifest_mac(payload, key)
    manifest = folder / 'manifest.json'
    manifest.write_text(json.dumps(payload, sort_keys=True, indent=2) + '\n')
    os.chmod(manifest, 0o600)
    print('snapshot=encrypted', flush=True)
    print(f'snapshot_id={identifier}', flush=True)


def verify(folder, key):
    """Authenticate a snapshot and its members without extracting it."""
    manifest = json.loads((folder / 'manifest.json').read_text())
    require(manifest.get('schema') == SCHEMA, 'snapshot_schema_invalid')
    mac = manifest.pop('hmac_sha256', None)
    require(isinstance(mac, str) and RECOVERY.hmac.compare_digest(mac, RECOVERY.manifest_mac(manifest, key)),
            'snapshot_authentication_failed')
    return manifest


def main():
    parser = RECOVERY.PrivateArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True)
    parser.add_argument('--service-user', required=True)
    parser.add_argument('--output-root', required=True)
    parser.add_argument('--key-root', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--final', action='store_true',
                        help='cutover: leave the old install stopped and disabled after a successful archive')
    parser.add_argument('--confirm-final-stop', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    require(not args.final or args.confirm_final_stop, 'final_stop_confirmation_required')
    ctx = inputs(args)
    ctx['final'] = bool(args.final)
    live = preflight(ctx)
    if args.final:
        print('snapshot_mode=final', flush=True)
    if not args.apply:
        print('snapshot_mutation=disabled', flush=True)
        return
    snapshot(ctx, live)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('snapshot_command=failed', flush=True)
        known = isinstance(error, (SnapshotError, RECOVERY.RecoveryError))
        print('failure_category=' + (str(error)[:80] if known else 'unexpected_error'), flush=True)
        sys.exit(2)
