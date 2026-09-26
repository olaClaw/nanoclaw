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

    def test_cli_errors_are_redacted(self):
        secret = '/opt/secret-fixture/private-path'
        for script in ('legacy-snapshot.py', 'compose-import-legacy.py'):
            with self.subTest(script):
                result = subprocess.run([sys.executable, str(Path(__file__).with_name(script)),
                                         '--project-root', secret], capture_output=True, text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('failure_category=invalid_arguments', result.stdout)
                self.assertNotIn(secret, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
