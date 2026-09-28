import importlib.util
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name('compose-portable.py')
SPEC = importlib.util.spec_from_file_location('compose_portable', MODULE_PATH)
PORTABLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PORTABLE)
RECOVERY = PORTABLE.RECOVERY
LEGACY = PORTABLE.LEGACY

REVISION = 'a' * 40
SECRET = 'fixture-secret-value-5d1e'


def central_db(path, groups=1):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE schema_version (version INTEGER, name TEXT)')
        db.executemany('INSERT INTO schema_version VALUES (?, ?)', [(1, 'initial'), (2, 'chat-sdk-state')])
        for table in LEGACY.SNAPSHOT.COUNT_TABLES:
            db.execute(f'CREATE TABLE {table} (id TEXT)')
        db.executemany('INSERT INTO agent_groups VALUES (?)', [(f'ag-{i}',) for i in range(groups)])
        db.execute("ALTER TABLE sessions ADD COLUMN container_status TEXT")
        db.execute("INSERT INTO sessions VALUES ('sess-1', 'running')")
    db.close()


def inbound_db(path):
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE messages_in (id TEXT, kind TEXT, status TEXT)')
        db.executemany('INSERT INTO messages_in VALUES (?, ?, ?)',
                       [('t1', 'task', 'pending'), ('c1', 'chat', 'pending'), ('c2', 'chat', 'completed')])
    db.close()


class Fixture:
    """A verified backup of a source install, and a target install beside it."""

    def __init__(self, root):
        self.root = root
        source = root / 'source-state'
        (source / 'data').mkdir(parents=True)
        (source / 'release.json').write_text(json.dumps({'revision': REVISION, 'version': '2.4.0'}))
        (source / 'data/upgrade-state.json').write_text(json.dumps({'commit': REVISION}))
        central_db(source / 'data/v2.db', groups=2)
        inbound_db(source / 'data/v2-sessions/ag-0/sess-1/inbound.db')
        (source / 'data/model-settings.json').write_text('{"endpoint": "source machine"}')
        (source / 'data/dashboard').mkdir()
        (source / 'data/dashboard/id-key').write_text('source key')
        (source / 'groups/main').mkdir(parents=True)
        (source / 'groups/main/notes.md').write_text('source notes')
        (source / 'signal/data').mkdir(parents=True)
        (source / 'signal/data/account').write_text('source signal identity')
        (source / 'proxy/letsencrypt').mkdir(parents=True)
        (source / 'proxy/letsencrypt/dns-token').write_text(SECRET)
        (source / 'dashboard').mkdir()
        (source / 'dashboard/admin.json').write_text('source admin')
        env = root / 'source-env'
        env.write_text(f'NANOCLAW_HOST_IMAGE=source-image\nSIGNAL_ACCOUNT=+15550100001\n'
                       f'TELEGRAM_BOT_TOKEN={SECRET}\nONECLI_API_KEY=onecli-key\nTZ=Europe/Rome\n'
                       f'OPENCODE_BASE_URL=http://SOURCE_LLM/v1\nOPENCODE_MODEL=openai/source-model\n')
        onecli = root / 'source-onecli'
        onecli.mkdir()
        (onecli / 'vault').write_text('source vault')
        extras = {}
        for name in RECOVERY.PRIVATE_INPUTS.values():
            path = root / ('source-' + name.replace('/', '-'))
            path.mkdir() if name.endswith('mail-downloads') else path.write_text(f'source {name}')
            extras[name] = path
        self.backup = root / 'backups/source'
        self.backup.mkdir(parents=True, mode=0o700)
        os.chmod(root / 'backups', 0o700)
        self.keys = root / 'keys'
        self.keys.mkdir(mode=0o700)
        self.backup_key = self.keys / 'source.key'
        self.backup_key.write_text('e' * 64 + '\n')
        os.chmod(self.backup_key, 0o600)
        archive, dump = root / 'state.tar', root / 'postgres.dump'
        dump.write_bytes(b'fixture-pg-dump')
        RECOVERY.archive_sources(archive, source, env, onecli, extras)
        payload = {'schema': RECOVERY.SCHEMA, 'revision': REVISION, 'created_utc': '20260101T000000Z',
                   'state': RECOVERY.encrypt(archive, self.backup / 'state.tar.enc', self.backup_key),
                   'postgres': RECOVERY.encrypt(dump, self.backup / 'postgres.dump.enc', self.backup_key)}
        payload['hmac_sha256'] = RECOVERY.manifest_mac(payload, self.backup_key)
        (self.backup / 'manifest.json').write_text(json.dumps(payload))
        self.exports = root / 'exports'
        self.exports.mkdir(mode=0o700)
        self.export_keys = root / 'export-keys'
        self.export_keys.mkdir(mode=0o700)

    def export(self):
        with redirect_stdout(io.StringIO()) as out:
            identifier = PORTABLE.export(Namespace(backup_dir=str(self.backup), backup_key=str(self.backup_key),
                                                   export_root=str(self.exports), key_root=str(self.export_keys)))
        return identifier, out.getvalue()


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_one_file_with_a_separate_key_and_no_machine_state(self):
        identifier, output = self.fixture.export()
        self.assertRegex(identifier, PORTABLE.EXPORT_ID)
        bundle = self.fixture.exports / f'{identifier}.ncx'
        key = self.fixture.export_keys / f'{identifier}.key'
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(key.read_text(), self.fixture.backup_key.read_text())
        self.assertEqual([p.name for p in self.fixture.exports.iterdir()], [bundle.name])
        with tarfile.open(bundle) as stream:
            self.assertEqual(stream.getnames(), list(PORTABLE.PARTS))
        self.assertNotIn(key.read_text().strip().encode(), bundle.read_bytes())
        self.assertNotIn(SECRET, output)

        work = Path(self.temp.name) / 'work'
        work.mkdir(mode=0o700)
        manifest = PORTABLE.open_bundle(bundle, key, work)
        self.assertEqual((manifest['revision'], manifest['version']), (REVISION, '2.4.0'))
        self.assertEqual(manifest['migrations'], ['chat-sdk-state', 'initial'])
        self.assertEqual(manifest['counts']['agent_groups'], 2)
        RECOVERY.decrypt(work / 'state.tar.enc', work / 'state.tar', key, manifest['state'])
        with tarfile.open(work / 'state.tar') as stream:
            names = set(stream.getnames())
        self.assertIn('state/groups/main/notes.md', names)
        self.assertIn('state/signal/data/account', names)
        self.assertIn('onecli-data/vault', names)
        for gone in ('state/proxy/letsencrypt/dns-token', 'state/dashboard/admin.json',
                     'state/data/model-settings.json', 'state/data/dashboard/id-key'):
            self.assertNotIn(gone, names)

    def test_a_tampered_or_foreign_bundle_is_refused(self):
        identifier, _ = self.fixture.export()
        bundle = self.fixture.exports / f'{identifier}.ncx'
        key = self.fixture.export_keys / f'{identifier}.key'
        other = self.fixture.export_keys / 'other.key'
        other.write_text('d' * 64 + '\n')
        os.chmod(other, 0o600)
        for n, (path, key_file, code) in enumerate(((bundle, other, 'bundle_authentication_failed'),)):
            work = Path(self.temp.name) / f'work-{n}'
            work.mkdir(mode=0o700)
            with self.assertRaisesRegex(PORTABLE.PortableError, code):
                PORTABLE.open_bundle(path, key_file, work)
        extra = Path(self.temp.name) / 'extra.ncx'
        with tarfile.open(bundle) as inp, tarfile.open(extra, 'w:') as out:
            for item in inp:
                out.addfile(item, inp.extractfile(item))
            evil = tarfile.TarInfo('../escape')
            evil.size = 1
            out.addfile(evil, io.BytesIO(b'x'))
        work = Path(self.temp.name) / 'work-extra'
        work.mkdir(mode=0o700)
        with self.assertRaisesRegex(PORTABLE.PortableError, 'bundle_member_unexpected'):
            PORTABLE.open_bundle(extra, key, work)


class EnvironmentTests(unittest.TestCase):
    TARGET = ('NANOCLAW_HOST_IMAGE=target-image\nNANOCLAW_INSTALL_ID=target\nOPENCODE_BASE_URL=http://TARGET_LLM/v1\n'
              'SIGNAL_ACCOUNT=+15550100002\n# kept comment\n')
    SOURCE = {'NANOCLAW_HOST_IMAGE': 'source-image', 'SIGNAL_ACCOUNT': '+15550100001', 'TELEGRAM_BOT_TOKEN': SECRET,
              'ONECLI_API_KEY': 'onecli-key', 'TZ': 'Europe/Rome', 'OPENCODE_BASE_URL': 'http://SOURCE_LLM/v1'}

    def test_rehearsal_carries_settings_but_never_an_identity(self):
        merged = LEGACY.parse_env(PORTABLE.merge_env(self.TARGET, self.SOURCE, 'rehearsal'))
        self.assertEqual(merged['NANOCLAW_HOST_IMAGE'], 'target-image')
        self.assertEqual(merged['OPENCODE_BASE_URL'], 'http://TARGET_LLM/v1')
        self.assertEqual((merged['ONECLI_API_KEY'], merged['TZ']), ('onecli-key', 'Europe/Rome'))
        self.assertNotIn('SIGNAL_ACCOUNT', merged)
        self.assertNotIn('TELEGRAM_BOT_TOKEN', merged)

    def test_migration_carries_the_identities(self):
        text = PORTABLE.merge_env(self.TARGET, self.SOURCE, 'migration')
        merged = LEGACY.parse_env(text)
        self.assertEqual((merged['SIGNAL_ACCOUNT'], merged['TELEGRAM_BOT_TOKEN']), ('+15550100001', SECRET))
        self.assertEqual(merged['NANOCLAW_INSTALL_ID'], 'target')
        self.assertIn('# kept comment', text)


class ImportApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.root = Path(self.temp.name)
        self.fixture = Fixture(root)
        identifier, _ = self.fixture.export()
        self.bundle = self.fixture.exports / f'{identifier}.ncx'
        self.key = self.fixture.export_keys / f'{identifier}.key'
        state = self.state = root / 'srv/nanoclaw'
        (state / 'data').mkdir(parents=True)
        central_db(state / 'data/v2.db', groups=0)
        (state / 'data/upgrade-state.json').write_text(json.dumps({'commit': REVISION, 'target': True}))
        (state / 'data/model-settings.json').write_text('{"endpoint": "target machine"}')
        (state / 'data/dashboard').mkdir()
        (state / 'data/dashboard/id-key').write_text('target key')
        (state / 'groups').mkdir()
        (state / 'signal').mkdir()
        self.project = root / 'project'
        self.project.mkdir()
        self.env = self.project / '.env'
        self.mail, self.calendar = root / 'mail.json', root / 'calendar.json'
        for path in (self.mail, self.calendar):
            path.write_text('target broker')
        self.env.write_text(f'NANOCLAW_INSTALL_ID=target\nINFOMANIAK_BROKER_CONFIG_FILE={self.mail}\n'
                            f'NEXTCLOUD_BROKER_CONFIG_FILE={self.calendar}\n')
        self.work = root / 'work'
        self.work.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def context(self, mode):
        plain = self.work / 'txn/plain'
        plain.mkdir(parents=True, mode=0o700)
        manifest = PORTABLE.open_bundle(self.bundle, self.key, plain)
        archive, dump = plain / 'state.tar', plain / 'postgres.dump'
        RECOVERY.decrypt(plain / 'state.tar.enc', archive, self.key, manifest['state'])
        RECOVERY.decrypt(plain / 'postgres.dump.enc', dump, self.key, manifest['postgres'])
        with tarfile.open(archive) as stream:
            source_env = LEGACY.parse_env(stream.extractfile('env').read().decode())
            brokers = {name: stream.extractfile(name).read() for name in PORTABLE.BROKER_INPUTS}
        return dict(project=self.project, state=self.state, txn=self.work / 'txn', plain=plain, archive=archive,
                    dump=dump, manifest=manifest, merged_env=PORTABLE.merge_env(self.env.read_text(), source_env, mode),
                    env_file=self.env, brokers=brokers, broker_targets={'private/mail-config': self.mail,
                                                                          'private/calendar-config': self.calendar},
                    empty=True, target_install='target')

    def apply(self, mode):
        restored = []
        output = io.StringIO()
        with (patch.object(LEGACY, 'volume_names', return_value=('onecli-data', 'onecli-pgdata')),
              patch.object(LEGACY, 'stop_stack'),
              patch.object(LEGACY, 'restore_onecli', side_effect=lambda *a: restored.append(sorted(p.name for p in a[2].iterdir()))),
              patch.object(LEGACY, 'compose', return_value=(0, '')),
              patch.object(LEGACY, 'services_healthy', return_value=True),
              patch.object(LEGACY, 'chown_tree'),
              patch.object(LEGACY, 'atomic_write', side_effect=lambda path, content, *_: path.write_bytes(content)),
              redirect_stdout(output)):
            PORTABLE.apply_import(self.context(mode), mode)
        return output.getvalue(), restored

    def test_rehearsal_copy_pauses_tasks_and_keeps_signal_and_machine_state_out(self):
        output, restored = self.apply('rehearsal')
        self.assertIn('import=healthy', output)
        self.assertIn('tasks_paused=1', output)
        self.assertIn('pending_chat_closed=1', output)
        self.assertEqual((self.state / 'groups/main/notes.md').read_text(), 'source notes')
        self.assertFalse((self.state / 'signal/data').exists())
        self.assertTrue((self.state / 'signal/attachments').is_dir())
        # The target keeps its release marker, model settings and public-ID key.
        self.assertTrue(json.loads((self.state / 'data/upgrade-state.json').read_text())['target'])
        self.assertIn('target machine', (self.state / 'data/model-settings.json').read_text())
        self.assertEqual((self.state / 'data/dashboard/id-key').read_text(), 'target key')
        env = LEGACY.parse_env(self.env.read_text())
        self.assertNotIn('SIGNAL_ACCOUNT', env)
        self.assertEqual(env['ONECLI_API_KEY'], 'onecli-key')
        self.assertEqual(self.mail.read_text(), 'source private/mail-config')
        self.assertEqual(restored, [['vault']])
        with sqlite3.connect(self.state / 'data/v2.db') as db:
            self.assertEqual(db.execute('SELECT container_status FROM sessions').fetchone()[0], 'stopped')
        with sqlite3.connect(self.state / 'data/v2-sessions/ag-0/sess-1/inbound.db') as db:
            self.assertEqual(dict(db.execute('SELECT id, status FROM messages_in')),
                             {'t1': 'paused', 'c1': 'completed', 'c2': 'completed'})
        self.assertNotIn(SECRET, output)
        # The replaced state waits in the transaction for a rollback.
        self.assertTrue((self.work / 'txn/previous/data/v2.db').is_file())
        self.assertFalse((self.work / 'txn/plain').exists())

    def test_migration_keeps_signal_tasks_and_identities(self):
        output, _ = self.apply('migration')
        self.assertIn('import=healthy', output)
        self.assertNotIn('tasks_paused', output)
        self.assertEqual((self.state / 'signal/data/account').read_text(), 'source signal identity')
        self.assertEqual(LEGACY.parse_env(self.env.read_text())['SIGNAL_ACCOUNT'], '+15550100001')
        with sqlite3.connect(self.state / 'data/v2-sessions/ag-0/sess-1/inbound.db') as db:
            self.assertEqual(db.execute("SELECT status FROM messages_in WHERE id = 't1'").fetchone()[0], 'pending')


class GuardTests(unittest.TestCase):
    def test_apply_needs_the_replace_and_source_stopped_confirmations(self):
        for args, ctx, code in ((['--mode', 'rehearsal'], {'empty': False}, 'replace_confirmation_required'),
                                (['--mode', 'migration'], {'empty': True}, 'source_stopped_confirmation_required')):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                plain = Path(temp) / 'plain'
                plain.mkdir()
                full = dict(ctx, plain=plain, txn=Path(temp))
                argv = ['import', '--project-root', temp, '--state-root', temp, '--target-backup-dir', temp,
                        '--target-backup-key', temp, '--bundle', temp, '--key-file', temp, '--work-root', temp,
                        '--apply', *args]
                with (patch.object(PORTABLE.os, 'geteuid', return_value=0),
                      patch.object(PORTABLE, 'preflight', return_value=full),
                      patch.object(PORTABLE, 'apply_import') as applied):
                    with self.assertRaisesRegex(PORTABLE.PortableError, code):
                        PORTABLE.main(argv)
                    applied.assert_not_called()
                self.assertFalse(plain.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
