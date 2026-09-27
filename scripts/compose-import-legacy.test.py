import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IMPORT = load('compose_import_legacy', 'compose-import-legacy.py')
SNAPSHOT = load('legacy_snapshot', 'legacy-snapshot.py')
RECOVERY = IMPORT.RECOVERY


def add(stream, name, *, data=None, kind='file', target=None):
    info = tarfile.TarInfo(name)
    if kind == 'dir':
        info.type = tarfile.DIRTYPE
        info.mode = 0o700
        stream.addfile(info)
    elif kind == 'link':
        info.type = tarfile.SYMTYPE
        info.linkname = target
        stream.addfile(info)
    elif kind == 'hardlink':
        info.type = tarfile.LNKTYPE
        info.linkname = target
        stream.addfile(info)
    else:
        payload = data or b'x'
        info.size = len(payload)
        info.mode = 0o600
        stream.addfile(info, io.BytesIO(payload))


def archive(path, extra=()):
    with tarfile.open(path, 'w:', format=tarfile.PAX_FORMAT) as stream:
        for name in ('data', 'groups', 'signal', 'onecli-data'):
            add(stream, name, kind='dir')
        add(stream, 'data/v2.db')
        add(stream, 'env', data=b'TZ=Europe/Rome\n')
        for item in extra:
            add(stream, *item[:1], **item[1])


class MemberTests(unittest.TestCase):
    def test_accepts_state_and_workspace_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.tar'
            archive(path, [('groups/g', {'kind': 'dir'}),
                           ('groups/g/.venv', {'kind': 'dir'}),
                           ('groups/g/.venv/python', {'kind': 'link', 'target': '/usr/bin/python3'}),
                           ('mail-config', {'data': b'{}'})])
            kinds = IMPORT.check_members(path)
            self.assertEqual(kinds['groups/g/.venv/python'], 'link')

    def test_rejects_escapes_unexpected_tops_and_links_outside_workspaces(self):
        cases = {
            'dotdot': [('groups/../etc', {'kind': 'dir'})],
            'top': [('home', {'kind': 'dir'})],
            'link_top': [('signal/key', {'kind': 'link', 'target': '/etc/shadow'})],
            'through_link': [('groups/l', {'kind': 'link', 'target': '/tmp'}), ('groups/l/f', {})],
        }
        for label, extra in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'a.tar'
                archive(path, extra)
                with self.assertRaises(IMPORT.ImportError_):
                    IMPORT.check_members(path)

    def test_requires_core_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.tar'
            with tarfile.open(path, 'w:') as stream:
                add(stream, 'data', kind='dir')
            with self.assertRaises(IMPORT.ImportError_):
                IMPORT.check_members(path)


class HardlinkTests(unittest.TestCase):
    def test_hardlink_to_earlier_file_in_same_area_is_accepted_and_extracted_as_one_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.tar'
            archive(path, [('groups/g', {'kind': 'dir'}),
                           ('groups/g/pkg-a', {'data': b'shared'}),
                           ('groups/g/pkg-b', {'kind': 'hardlink', 'target': 'groups/g/pkg-a'})])
            self.assertEqual(IMPORT.check_members(path)['groups/g/pkg-b'], 'hardlink')
            target = Path(tmp) / 'out'
            target.mkdir()
            IMPORT.extract(path, target)
            self.assertEqual((target / 'groups/g/pkg-a').stat().st_ino, (target / 'groups/g/pkg-b').stat().st_ino)

    def test_hardlink_outside_its_area_or_to_a_missing_file_is_rejected(self):
        for label, extra in {
            'cross_area': [('groups/x', {'kind': 'hardlink', 'target': 'data/v2.db'})],
            'missing': [('groups/x', {'kind': 'hardlink', 'target': 'groups/nope'})],
            'escape': [('groups/x', {'kind': 'hardlink', 'target': '../etc/passwd'})],
        }.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'a.tar'
                archive(path, extra)
                with self.assertRaises(IMPORT.ImportError_):
                    IMPORT.check_members(path)


class RewriteTests(unittest.TestCase):
    def test_mail_mcp_url_points_at_compose_service_and_keeps_headers(self):
        raw = json.dumps({
            'infomaniak_mail_readonly': {'type': 'http', 'url': 'http://host.docker.internal:18765/mcp',
                                         'headers': {'Authorization': 'Bearer t'}},
            'other': {'type': 'http', 'url': 'https://example.invalid/mcp'},
        })
        new, changed = IMPORT.rewrite_mcp_servers(raw)
        servers = json.loads(new)
        self.assertEqual(changed, 1)
        self.assertEqual(servers['infomaniak_mail_readonly']['url'], IMPORT.COMPOSE_BROKER_URL)
        self.assertEqual(servers['infomaniak_mail_readonly']['headers'], {'Authorization': 'Bearer t'})
        self.assertEqual(servers['other']['url'], 'https://example.invalid/mcp')
        self.assertEqual(IMPORT.rewrite_mcp_servers(new), (new, 0))
        self.assertEqual(IMPORT.rewrite_mcp_servers(None), (None, 0))

    def test_mail_config_keeps_credentials_and_moves_listener_and_downloads(self):
        raw = {'email_address': 'a@example.invalid', 'device_password': 'p', 'broker_token': 't',
               'bind': '192.0.2.1', 'port': 9999,
               'download_dir': '/opt/legacy/nanoclaw/groups/main/downloads/infomaniak', 'max_message_bytes': 5}
        out = IMPORT.translate_mail_config(raw, '/opt/legacy/nanoclaw', '/srv/nanoclaw')
        self.assertEqual(out['bind'], '0.0.0.0')
        self.assertEqual(out['port'], 18765)
        self.assertEqual(out['download_dir'], '/srv/nanoclaw/groups/main/downloads/infomaniak')
        for key in ('email_address', 'device_password', 'broker_token', 'max_message_bytes'):
            self.assertEqual(out[key], raw[key])
        for bad in ('/opt/legacy/elsewhere', '/opt/legacy/nanoclaw/data/x'):
            with self.subTest(bad), self.assertRaises(IMPORT.ImportError_):
                IMPORT.translate_mail_config({**raw, 'download_dir': bad}, '/opt/legacy/nanoclaw', '/srv/nanoclaw')

    def test_env_merge_rehearsal_never_carries_identities(self):
        target = 'NANOCLAW_HOST_IMAGE=x\nOPENCODE_MODEL=old\nSIGNAL_ACCOUNT=+0\n# note\n'
        legacy = {'OPENCODE_MODEL': 'm', 'TZ': 'Europe/Rome', 'SIGNAL_ACCOUNT': '+1', 'TELEGRAM_BOT_TOKEN': 't',
                  'SIGNAL_CLI_PATH': '/x'}
        merged = IMPORT.parse_env(IMPORT.merge_env(target, legacy, 'rehearsal', {'INFOMANIAK_DOWNLOAD_DIR': '/d'}))
        self.assertEqual(merged['OPENCODE_MODEL'], 'm')
        self.assertEqual(merged['TZ'], 'Europe/Rome')
        self.assertEqual(merged['INFOMANIAK_DOWNLOAD_DIR'], '/d')
        self.assertEqual(merged['NANOCLAW_HOST_IMAGE'], 'x')
        for key in ('SIGNAL_ACCOUNT', 'TELEGRAM_BOT_TOKEN', 'SIGNAL_CLI_PATH'):
            self.assertNotIn(key, merged)

    def test_env_merge_cutover_carries_identities_once(self):
        target = 'OPENCODE_MODEL=a\nOPENCODE_MODEL=b\n'
        merged_text = IMPORT.merge_env(target, {'SIGNAL_ACCOUNT': '+1', 'TELEGRAM_BOT_TOKEN': 't',
                                                'OPENCODE_MODEL': 'm'}, 'cutover', {})
        self.assertEqual(merged_text.count('OPENCODE_MODEL='), 1)
        merged = IMPORT.parse_env(merged_text)
        self.assertEqual((merged['SIGNAL_ACCOUNT'], merged['TELEGRAM_BOT_TOKEN']), ('+1', 't'))
        with self.assertRaises(IMPORT.ImportError_):
            IMPORT.merge_env('', {'TZ': 'a\nINJECT=1'}, 'cutover', {})


class DatabaseTests(unittest.TestCase):
    def test_rehearsal_pauses_tasks_and_closes_chat_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'inbound.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE messages_in (id TEXT, kind TEXT, status TEXT)')
                db.executemany('INSERT INTO messages_in VALUES (?, ?, ?)', [
                    ('1', 'task', 'pending'), ('2', 'task', 'completed'), ('3', 'chat', 'pending'),
                    ('4', 'chat', 'completed'), ('5', 'task', 'paused')])
            self.assertEqual(IMPORT.neutralize_session(path), (1, 1))
            with sqlite3.connect(path) as db:
                rows = dict(db.execute('SELECT id, status FROM messages_in'))
            self.assertEqual(rows, {'1': 'paused', '2': 'completed', '3': 'completed', '4': 'completed',
                                    '5': 'paused'})

    def test_central_fix_rewrites_mail_url_and_forgets_running_containers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'v2.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE container_configs (agent_group_id TEXT, mcp_servers TEXT)')
                db.execute('CREATE TABLE sessions (id TEXT, container_status TEXT)')
                db.execute('INSERT INTO container_configs VALUES (?, ?)', ('g', json.dumps(
                    {'infomaniak_mail_readonly': {'type': 'http', 'url': 'http://localhost:1/mcp'}})))
                db.execute('INSERT INTO container_configs VALUES (?, NULL)', ('h',))
                db.executemany('INSERT INTO sessions VALUES (?, ?)', [('a', 'running'), ('b', 'stopped')])
            self.assertEqual(IMPORT.fix_central_db(path), (1, 1))
            with sqlite3.connect(path) as db:
                url = json.loads(db.execute("SELECT mcp_servers FROM container_configs WHERE agent_group_id='g'")
                                 .fetchone()[0])['infomaniak_mail_readonly']['url']
                statuses = {r[0] for r in db.execute('SELECT container_status FROM sessions')}
            self.assertEqual(url, IMPORT.COMPOSE_BROKER_URL)
            self.assertEqual(statuses, {'stopped'})


class ExtractionTests(unittest.TestCase):
    def test_links_are_recreated_verbatim_and_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.tar'
            archive(path, [('groups/g', {'kind': 'dir'}),
                           ('groups/g/python', {'kind': 'link', 'target': '/usr/bin/python3'})])
            target = Path(tmp) / 'out'
            target.mkdir()
            IMPORT.extract(path, target)
            self.assertEqual(os.readlink(target / 'groups/g/python'), '/usr/bin/python3')
            self.assertTrue((target / 'data/v2.db').is_file())


class SnapshotTests(unittest.TestCase):
    def test_install_slug_matches_checkout_hash_or_override(self):
        import hashlib
        root = Path('/opt/legacy/nanoclaw')
        self.assertEqual(SNAPSHOT.install_slug(root, {}), hashlib.sha1(str(root).encode()).hexdigest()[:8])
        self.assertEqual(SNAPSHOT.install_slug(root, {'NANOCLAW_INSTALL_ID': 'andy'}), 'andy')
        with self.assertRaises(SNAPSHOT.SnapshotError):
            SNAPSHOT.install_slug(root, {'NANOCLAW_INSTALL_ID': 'Bad Id'})

    def test_manifest_authentication_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, key = Path(tmp) / 's', Path(tmp) / 'k'
            folder.mkdir()
            key.write_text('ab' * 32 + '\n')
            payload = {'schema': SNAPSHOT.SCHEMA, 'counts': {'agent_groups': 3}}
            payload['hmac_sha256'] = RECOVERY.manifest_mac(dict(payload), key)
            (folder / 'manifest.json').write_text(json.dumps(payload))
            self.assertEqual(SNAPSHOT.verify(folder, key)['counts'], {'agent_groups': 3})
            payload['counts'] = {'agent_groups': 4}
            (folder / 'manifest.json').write_text(json.dumps(payload))
            with self.assertRaises(SNAPSHOT.SnapshotError):
                SNAPSHOT.verify(folder, key)

    def test_failed_snapshot_leaves_no_folder_or_key_behind(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            out, keys = Path(tmp) / 'out', Path(tmp) / 'keys'
            out.mkdir()
            keys.mkdir()
            ctx = {'project': Path(tmp), 'user': mock.Mock(pw_uid=1000, pw_gid=1000, pw_name='svc'),
                   'output_root': out, 'key_root': keys, 'host_unit': 'nanoclaw-v2-x.service', 'slug': 'x',
                   'mail_config': None}
            live = {'head': 'a' * 40}
            with mock.patch.object(SNAPSHOT, 'run', side_effect=SNAPSHOT.SnapshotError('command_failed')), \
                    mock.patch('sys.stdout', new=io.StringIO()):
                with self.assertRaises(SNAPSHOT.SnapshotError):
                    SNAPSHOT.snapshot(ctx, live)
            self.assertEqual(list(out.iterdir()), [])
            self.assertEqual(list(keys.iterdir()), [])

    def test_hardlinks_are_kept_by_the_snapshot_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'groups'
            root.mkdir()
            (root / 'a').write_text('x')
            os.link(root / 'a', root / 'b')
            with tarfile.open(Path(tmp) / 's.tar', 'w:') as stream:
                stream.add(root, arcname='groups', filter=SNAPSHOT.archive_filter)
            with tarfile.open(Path(tmp) / 's.tar', 'r:') as stream:
                self.assertTrue(stream.getmember('groups/b').islnk())

    def test_cli_errors_are_redacted(self):
        secret = '/opt/secret-fixture/private-path'
        for script in ('legacy-snapshot.py', 'compose-import-legacy.py'):
            with self.subTest(script):
                result = subprocess.run([sys.executable, str(Path(__file__).with_name(script)),
                                         '--project-root', secret], capture_output=True, text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('failure_category=invalid_arguments', result.stdout)
                self.assertNotIn(secret, result.stdout + result.stderr)


# Options accepted by `docker compose <subcommand>` in Compose 5.5.1 (the target host's version).
COMPOSE_OPTIONS = {
    'config': {'--dry-run', '--environment', '--format', '--hash', '--images', '--lock-image-digests', '--models',
               '--networks', '--no-consistency', '--no-env-resolution', '--no-interpolate', '--no-normalize',
               '--no-path-resolution', '-o', '--output', '--profiles', '-q', '--quiet', '--resolve-image-digests',
               '--services', '--variables', '--volumes'},
    'create': {'--build', '--dry-run', '--force-recreate', '--no-build', '--no-recreate', '--pull', '--quiet-pull',
               '--remove-orphans', '--scale', '-y', '--yes'},
    'exec': {'-d', '--detach', '--dry-run', '-e', '--env', '--index', '-T', '--no-tty', '--privileged', '-u',
             '--user', '-w', '--workdir'},
    'ps': {'-a', '--all', '--dry-run', '--filter', '--format', '--no-trunc', '--orphans', '-q', '--quiet',
           '--services', '--status'},
    'rm': {'--dry-run', '-f', '--force', '-s', '--stop', '-v', '--volumes'},
    'stop': {'--dry-run', '-t', '--timeout'},
    'up': {'--abort-on-container-exit', '--always-recreate-deps', '--build', '-d', '--detach', '--dry-run',
           '--force-recreate', '--no-build', '--no-deps', '--no-recreate', '--no-start', '--pull', '--quiet-pull',
           '--remove-orphans', '--scale', '-t', '--timeout', '--wait', '--wait-timeout', '-y', '--yes'},
}


def compose_calls(path):
    """Literal (subcommand, options) of every compose(project, ...) call in a script."""
    import ast
    calls = []
    for node in ast.walk(ast.parse(path.read_text())):
        if (isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'compose' and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)):
            literals = [a.value for a in node.args[1:] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            subcommand, rest = literals[0], literals[1:]
            options = []
            for arg in rest:
                if arg.startswith('--'):
                    options.append(arg)
                elif arg.startswith('-') and len(arg) > 1:
                    options.extend(f'-{flag}' for flag in arg[1:])
                elif subcommand == 'exec':
                    break  # the command run inside the container starts here
            calls.append((subcommand, options))
    return calls


class FinalSnapshotTests(unittest.TestCase):
    """legacy-snapshot.py --final: the old install stays down only after a complete archive."""

    def fixture(self, tmp):
        from unittest import mock
        root = Path(tmp)
        project = root / 'project'
        for d in ('data', 'groups', 'signal', 'onecli', 'out', 'keys'):
            (root / d if d in ('signal', 'onecli', 'out', 'keys') else project / d).mkdir(parents=True)
        (project / '.env').write_text('TZ=Europe/Rome\n')
        with sqlite3.connect(project / 'data/v2.db') as db:
            for table in SNAPSHOT.COUNT_TABLES:
                db.execute(f'CREATE TABLE {table} (id TEXT)')
            db.execute('CREATE TABLE schema_version (name TEXT)')
            db.execute("INSERT INTO schema_version VALUES ('initial-v2-schema')")
        ctx = {'project': project, 'user': mock.Mock(pw_uid=1000, pw_gid=1000, pw_name='svc'),
               'output_root': root / 'out', 'key_root': root / 'keys', 'host_unit': 'nanoclaw-v2-x.service',
               'slug': 'x', 'mail_config': None, 'env_file': project / '.env', 'signal': root / 'signal',
               'config_dir': root / 'none'}
        live = {'head': 'a' * 40, 'migrations': ['initial-v2-schema'], 'onecli': 'onecli-c', 'postgres': 'pg-c',
                'onecli_data': root / 'onecli', 'pg_user': 'onecli', 'pg_db': 'onecli'}
        return ctx, live

    def run_snapshot(self, final, fail_dump=False):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            ctx, live = self.fixture(tmp)
            ctx['final'] = final
            calls = []

            def fake_run(argv, *, user=None, check=True, timeout=600, stdout=None, cwd=None):
                calls.append(tuple(argv))
                if argv[:2] == ['docker', 'exec'] and 'pg_dump' in argv:
                    if fail_dump:
                        raise SNAPSHOT.SnapshotError('command_failed')
                    stdout.write(b'PGDMP fixture')
                if argv[:2] == ['docker', 'inspect']:
                    return 0, 'unless-stopped'
                if 'is-enabled' in argv:
                    return 0, ''
                return 0, ''

            def fake_encrypt(source, destination, key):
                destination.write_bytes(source.read_bytes())
                return {'encrypted_sha256': 'e' * 64, 'plain_sha256': 'p' * 64, 'bytes': destination.stat().st_size}

            listing = mock.Mock(returncode=0, stdout=b'; TABLE public agents')
            active = iter([False] + [True] * 50)
            with mock.patch.object(SNAPSHOT, 'run', side_effect=fake_run), \
                    mock.patch.object(SNAPSHOT.RECOVERY, 'encrypt', side_effect=fake_encrypt), \
                    mock.patch.object(SNAPSHOT, 'unit_active', side_effect=lambda *a: next(active)), \
                    mock.patch.object(SNAPSHOT.subprocess, 'run', return_value=listing), \
                    mock.patch('sys.stdout', new=io.StringIO()) as out:
                try:
                    SNAPSHOT.snapshot(ctx, live)
                    error = None
                except SNAPSHOT.SnapshotError as exc:
                    error = exc
            rollback = sorted((ctx['output_root']).glob('*.rollback-old-install.sh'))
            script = rollback[0].read_text() if rollback else None
            folders = [p for p in ctx['output_root'].iterdir() if p.is_dir()]
            return out.getvalue(), calls, script, folders, error

    def test_final_snapshot_leaves_the_old_install_down_with_a_rollback(self):
        output, calls, script, folders, error = self.run_snapshot(final=True)
        self.assertIsNone(error)
        self.assertIn('original_install=stopped_and_disabled', output)
        self.assertIn('snapshot=encrypted', output)
        self.assertFalse(any(c[:2] == ('docker', 'start') or c[-2:-1] == ('start',) for c in calls))
        self.assertIn(('docker', 'update', '--restart=no', 'pg-c'), calls)
        self.assertIn(('docker', 'update', '--restart=no', 'onecli-c'), calls)
        self.assertIn(('systemctl', '--user', 'disable', 'nanoclaw-v2-x.service'), calls)
        self.assertIn('docker update --restart=unless-stopped pg-c', script)
        self.assertIn('systemctl --user enable nanoclaw-v2-x.service', script)
        self.assertIn('systemctl --user start nanoclaw-v2-x.service', script)
        self.assertEqual(len(folders), 1)

    def test_final_snapshot_that_fails_before_the_archive_restarts_the_old_install(self):
        output, calls, script, folders, error = self.run_snapshot(final=True, fail_dump=True)
        self.assertIsNotNone(error)
        self.assertIn('original_install=restarted', output)
        self.assertIn(('docker', 'start', 'onecli-c'), calls)
        self.assertIn(('systemctl', '--user', 'start', 'nanoclaw-v2-x.service'), calls)
        self.assertFalse(any('disable' in c for c in calls))
        self.assertIsNone(script)
        self.assertEqual(folders, [])

    def test_normal_snapshot_restarts_and_never_disables(self):
        output, calls, script, _, error = self.run_snapshot(final=False)
        self.assertIsNone(error)
        self.assertIn('original_install=restarted', output)
        self.assertFalse(any('disable' in c or '--restart=no' in c for c in calls))
        self.assertIsNone(script)


class ComposeOptionTests(unittest.TestCase):
    def test_every_compose_call_uses_options_the_subcommand_accepts(self):
        scripts = ('compose-import-legacy.py', 'compose-rehearsal.py', 'compose-recovery.py',
                   'compose-release-update.py')
        seen = 0
        for script in scripts:
            for subcommand, options in compose_calls(Path(__file__).with_name(script)):
                seen += 1
                with self.subTest(script=script, subcommand=subcommand):
                    self.assertIn(subcommand, COMPOSE_OPTIONS)
                    self.assertEqual(set(options) - COMPOSE_OPTIONS[subcommand], set())
        self.assertGreater(seen, 20)

    def test_the_checker_rejects_the_flag_that_broke_the_first_rehearsal(self):
        self.assertNotIn('--no-deps', COMPOSE_OPTIONS['create'])


class RollbackTests(unittest.TestCase):
    def test_rollback_restores_previous_state_config_and_onecli_from_backup(self):
        from argparse import Namespace
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, state, txn, backup = root / 'project', root / 'state', root / 'txn', root / 'backup'
            for d in (project, state, txn / 'previous/data', txn / 'previous/groups', backup):
                d.mkdir(parents=True, mode=0o700)
            for d in (txn, txn / 'previous', backup):
                os.chmod(d, 0o700)
            mail = root / 'mail.json'
            (project / '.env').write_text(f'INFOMANIAK_BROKER_CONFIG_FILE={mail}\nTZ=imported\n')
            mail.write_text('{"imported": true}')
            (state / 'release.json').write_text(json.dumps({'revision': 'a' * 40}))
            (state / 'data').mkdir()
            (state / 'data/imported').write_text('real data copy')
            (txn / 'previous/data/upgrade-state.json').write_text(json.dumps({'commit': 'a' * 40}))
            (txn / 'previous/env').write_text(f'INFOMANIAK_BROKER_CONFIG_FILE={mail}\nTZ=synthetic\n')
            (txn / 'previous/mail-config').write_text('{"synthetic": true}')
            (backup / 'manifest.json').write_text(json.dumps({'revision': 'a' * 40}))
            (root / 'keys').mkdir(mode=0o700)
            key = root / 'keys/backup.key'
            key.write_text('ab' * 32)
            calls = []

            def stage(ns):
                target = Path(ns.target_dir)
                (target / 'onecli-data').mkdir(parents=True)
                (target / 'postgres.dump').write_bytes(b'dump')

            def fake_write(path, content, uid, gid, mode):
                path.write_bytes(content)

            args = Namespace(project_root=str(project), state_root=str(state), rollback_txn=str(txn),
                             target_backup_dir=str(backup), target_backup_key=str(key), apply=True,
                             confirm_restore_previous_state=True)
            with mock.patch.object(IMPORT, 'volume_names', return_value=('v-onecli', 'v-pg')), \
                    mock.patch.object(IMPORT, 'compose', side_effect=lambda p, *a, **k: calls.append(a) or (0, '')), \
                    mock.patch.object(IMPORT, 'restore_onecli', side_effect=lambda *a: calls.append(('restore', a))), \
                    mock.patch.object(IMPORT, 'services_healthy', return_value=True), \
                    mock.patch.object(IMPORT, 'atomic_write', side_effect=fake_write), \
                    mock.patch.object(IMPORT.RECOVERY, 'verify_or_stage', side_effect=stage), \
                    mock.patch('sys.stdout', new=io.StringIO()) as out:
                IMPORT.rollback(args)
            self.assertIn('rollback=healthy', out.getvalue())
            self.assertTrue((state / 'data/upgrade-state.json').is_file())
            self.assertFalse((state / 'data/imported').exists())
            self.assertEqual((txn / 'discarded/data/imported').read_text(), 'real data copy')
            self.assertIn('TZ=synthetic', (project / '.env').read_text())
            self.assertEqual(mail.read_text(), '{"synthetic": true}')
            restore = [c for c in calls if c and c[0] == 'restore'][0][1]
            self.assertEqual((restore[1].name, restore[2].name, restore[3]), ('postgres.dump', 'onecli-data', ('v-onecli', 'v-pg')))
            self.assertIn(('up', '-d', '--wait'), calls)
            self.assertFalse((txn / 'rollback-stage').exists())

    def test_rollback_refuses_an_incomplete_previous_state(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for d in ('project', 'state', 'txn/previous', 'backup'):
                (root / d).mkdir(parents=True, mode=0o700)
            os.chmod(root / 'txn', 0o700)
            os.chmod(root / 'backup', 0o700)
            (root / 'keys').mkdir(mode=0o700)
            (root / 'keys/k').write_text('ab' * 32)
            args = Namespace(project_root=str(root / 'project'), state_root=str(root / 'state'),
                             rollback_txn=str(root / 'txn'), target_backup_dir=str(root / 'backup'),
                             target_backup_key=str(root / 'keys/k'), apply=False, confirm_restore_previous_state=False)
            with self.assertRaisesRegex(IMPORT.ImportError_, 'previous_state_incomplete'):
                IMPORT.rollback(args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
