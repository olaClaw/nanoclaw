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
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name('compose-recovery.py')
SPEC = importlib.util.spec_from_file_location('compose_recovery', MODULE_PATH)
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)


class ComposeRecoveryTests(unittest.TestCase):
    def test_invalid_arguments_do_not_echo_values(self):
        sensitive = 'fixture-sensitive-content'
        result = subprocess.run([sys.executable, str(MODULE_PATH), 'verify',
                                 '--unexpected', sensitive], capture_output=True,
                                text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('failure_category=invalid_arguments', result.stdout)
        self.assertNotIn(sensitive, result.stdout + result.stderr)

    def test_agent_name_matches_short_and_hashed_driver_shapes(self):
        self.assertEqual(RECOVERY.expected_agent_name('preview', 'sess-1'),
                         'ncl-preview-sess-1')
        long_name = RECOVERY.expected_agent_name('preview', 'sess-' + 'a' * 90)
        self.assertTrue(long_name.startswith('ncl-preview-sess-'))
        self.assertEqual(len(long_name), 52)

    def test_archive_rejects_traversal_and_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / 'unsafe.tar'
            for member in ('../outside', 'state/link'):
                with tarfile.open(archive, 'w:') as stream:
                    entry = tarfile.TarInfo(member)
                    if member == 'state/link':
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = '../outside'
                    else:
                        entry.size = 1
                    stream.addfile(entry, io.BytesIO(b'x') if entry.isfile() else None)
                with self.assertRaises(RECOVERY.RecoveryError):
                    RECOVERY.checked_members(archive)

    def test_symlink_rules_allow_workspace_links_only(self):
        allowed = RECOVERY.symlink_allowed
        self.assertTrue(allowed('state/groups/main/.venv/bin/python', '/usr/bin/python3'))
        self.assertTrue(allowed('state/groups/main/current', 'releases/2'))
        self.assertTrue(allowed('state/data/container-skill', '/app/skills/example'))
        self.assertTrue(allowed('state/data/host-harness/x/node_modules/.bin/tool', '../pkg/bin/tool'))
        self.assertFalse(allowed('state/data/host-harness/x/node_modules/.bin/tool', '../../../../../groups/g'))
        self.assertFalse(allowed('state/data/a', '../../env'))
        self.assertTrue(allowed('state/proxy/letsencrypt/live/npm-1/cert.pem', '../../archive/npm-1/cert1.pem'))
        self.assertFalse(allowed('state/proxy/letsencrypt/live/npm-1/cert.pem', '/etc/letsencrypt/archive/npm-1/cert1.pem'))
        self.assertFalse(allowed('state/proxy/letsencrypt/live/npm-1/cert.pem', '../../../../data/v2.db'))
        self.assertFalse(allowed('state/proxy/data/x', '../letsencrypt/archive/y'))
        self.assertFalse(allowed('state/proxy/letsencrypt/live/x', ''))
        for name, target in (('state/data/x', '/etc/passwd'), ('state/data/x', '/app/../etc'),
                             ('state/link', '/usr/bin/python3'), ('state/signal/k', '/tmp/x'),
                             ('env', '/etc/passwd'), ('state/groups/main/empty', '')):
            with self.subTest(name=name, target=target):
                self.assertFalse(allowed(name, target))

    def test_archive_rejects_hardlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / 'unsafe-hardlink.tar'
            for target in ('not-in-archive', '../outside', 'state/release.json'):
                with tarfile.open(archive, 'w:') as stream:
                    entry = tarfile.TarInfo('private/mail-config')
                    entry.type = tarfile.LNKTYPE
                    entry.linkname = target
                    stream.addfile(entry)
                with self.assertRaisesRegex(RECOVERY.RecoveryError, 'archive_member_unsafe'):
                    RECOVERY.checked_members(archive)

    def test_extraction_preserves_numeric_owner_and_filters_unsafe_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            item = tarfile.TarInfo('state/data/example')
            item.uid = 1000
            item.gid = 1001
            item.uname = 'nonportable-user'
            item.gname = 'nonportable-group'
            item.mode = 0o660
            filtered = RECOVERY.preserve_numeric_metadata(item, temp)
            self.assertEqual((filtered.uid, filtered.gid, filtered.mode), (1000, 1001, 0o640))
            self.assertEqual((filtered.uname, filtered.gname), ('', ''))
            item.mode = 0o7755
            self.assertEqual(RECOVERY.preserve_numeric_metadata(item, temp).mode, 0o755)
            unsafe = tarfile.TarInfo('../outside')
            with self.assertRaises(tarfile.FilterError):
                RECOVERY.preserve_numeric_metadata(unsafe, temp)

    def test_running_profile_services_excludes_stopped_services(self):
        commands = []

        def mocked_compose(_project, *args):
            commands.append(args)
            if args[:2] == ('ps', '-q'):
                return b'container-id\n' if args[2] in ('nanoclaw', 'postgres') else b''
            return b''

        with patch.object(RECOVERY, 'compose', side_effect=mocked_compose):
            running = RECOVERY.running_profile_services(Path('/fixture'))
            self.assertEqual(running, ('nanoclaw', 'postgres'))
            RECOVERY.resume_profile_services(Path('/fixture'), running)
        restart = commands[-1]
        self.assertEqual(restart[:4], ('up', '-d', '--wait', '--no-deps'))
        self.assertIn('--no-recreate', restart)
        self.assertEqual(restart[-2:], ('nanoclaw', 'postgres'))
        self.assertNotIn('nextcloud-calendar', restart)

    def test_hardlink_to_earlier_file_under_same_member_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state'
            (state / 'data').mkdir(parents=True)
            (state / 'data/first').write_text('fixture')
            os.link(state / 'data/first', state / 'data/second')
            with tarfile.open(root / 'links.tar', 'w:') as stream:
                stream.add(state, arcname='state')
            with tarfile.open(root / 'links.tar', 'r:') as stream:
                member = stream.getmember('state/data/second')
                kinds = {'state/data/first': 'file'}
                self.assertTrue(member.islnk())
                self.assertTrue(RECOVERY.hardlink_target_ok(member, kinds))
                self.assertFalse(RECOVERY.hardlink_target_ok(member, {}))
                member.linkname = 'env'
                self.assertFalse(RECOVERY.hardlink_target_ok(member, {'env': 'file'}))

    def test_private_roots_reject_world_access(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'public'
            path.mkdir(mode=0o755)
            with self.assertRaisesRegex(RECOVERY.RecoveryError, 'private_directory_required'):
                RECOVERY.private_directory(path)

    def test_encrypted_backup_verifies_and_stages_without_leaking_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state-source'
            (state / 'data').mkdir(parents=True)
            (state / 'release.json').write_text(json.dumps({'revision': 'a' * 40}))
            (state / 'data/upgrade-state.json').write_text(json.dumps({'commit': 'a' * 40}))
            (state / 'data/container-skill').symlink_to('/app/skills/example')
            (state / 'groups/main/.venv/bin').mkdir(parents=True)
            (state / 'groups/main/.venv/bin/python').symlink_to('/usr/bin/python3')
            (state / 'data/pkg-a').write_text('shared package file')
            os.link(state / 'data/pkg-a', state / 'data/pkg-b')
            with sqlite3.connect(state / 'data/v2.db') as db:
                db.execute('CREATE TABLE example (id INTEGER)')
            env = root / 'env-source'
            sensitive = 'fixture-sensitive-content'
            env.write_text('EXAMPLE=' + sensitive + '\n')
            onecli = root / 'onecli-source'
            onecli.mkdir()
            (onecli / 'data').write_text(sensitive)
            extras = {}
            for name in RECOVERY.PRIVATE_INPUTS.values():
                path = root / name.replace('/', '-')
                if name.endswith('mail-downloads'):
                    path.mkdir()
                    (path / 'attachment').write_text(sensitive)
                else:
                    path.write_text(sensitive)
                extras[name] = path
            # Broker configuration may already live below the archived state root.
            included_private_file = state / 'included-private-file'
            included_private_file.write_text(sensitive)
            extras['private/mail-config'] = included_private_file
            backup = root / 'backup'
            backup.mkdir(mode=0o700)
            os.chmod(backup, 0o700)
            key_dir = root / 'keys'
            key_dir.mkdir(mode=0o700)
            key = key_dir / 'key'
            key.write_text('f' * 64 + '\n')
            os.chmod(key, 0o600)
            archive = root / 'source.tar'
            dump = root / 'postgres.dump'
            dump.write_bytes(b'fixture-pg-dump')
            RECOVERY.archive_sources(archive, state, env, onecli, extras)
            with tarfile.open(archive, 'r:') as stream:
                self.assertTrue(stream.getmember('private/mail-config').isfile())
                self.assertTrue(stream.getmember('state/data/container-skill').issym())
            state_checks = RECOVERY.encrypt(archive, backup / 'state.tar.enc', key)
            postgres_checks = RECOVERY.encrypt(dump, backup / 'postgres.dump.enc', key)
            payload = {'schema': RECOVERY.SCHEMA, 'revision': 'a' * 40,
                       'state': state_checks, 'postgres': postgres_checks}
            payload['hmac_sha256'] = RECOVERY.manifest_mac(payload, key)
            (backup / 'manifest.json').write_text(json.dumps(payload))
            output = io.StringIO()
            with redirect_stdout(output):
                RECOVERY.verify_or_stage(Namespace(action='verify', backup_dir=str(backup),
                                                   key_file=str(key)))
                RECOVERY.verify_or_stage(Namespace(action='stage', backup_dir=str(backup),
                                                   key_file=str(key), target_dir=str(root / 'staged'),
                                                   confirm_sensitive_plaintext=True))
            self.assertIn('backup_verify=ok', output.getvalue())
            self.assertIn('restore_stage=ok', output.getvalue())
            self.assertNotIn(sensitive, output.getvalue())
            self.assertEqual((root / 'staged/env').read_text(), env.read_text())
            self.assertEqual((root / 'staged/postgres.dump').read_bytes(), dump.read_bytes())
            self.assertEqual(os.readlink(root / 'staged/state/data/container-skill'),
                             '/app/skills/example')
            self.assertEqual((root / 'staged').stat().st_mode & 0o077, 0)
            self.assertEqual(os.readlink(root / 'staged/state/groups/main/.venv/bin/python'), '/usr/bin/python3')
            staged_a, staged_b = root / 'staged/state/data/pkg-a', root / 'staged/state/data/pkg-b'
            self.assertEqual(staged_a.stat().st_ino, staged_b.stat().st_ino)
            self.assertEqual(staged_b.read_text(), 'shared package file')
            payload['revision'] = 'b' * 40
            (backup / 'manifest.json').write_text(json.dumps(payload))
            with self.assertRaisesRegex(RECOVERY.RecoveryError, 'backup_authentication_failed'):
                RECOVERY.verify_or_stage(Namespace(action='verify', backup_dir=str(backup),
                                                   key_file=str(key)))

    def test_archive_skips_special_files_and_rejects_other_absolute_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state'
            (state / 'data').mkdir(parents=True)
            os.mkfifo(state / 'data/transient.pipe')
            with tarfile.open(root / 'special.tar', 'w:') as stream:
                stream.add(state, arcname='state', filter=lambda item: item if item.isfile() or item.isdir() or item.issym() else None)
            with tarfile.open(root / 'special.tar', 'r:') as stream:
                self.assertNotIn('state/data/transient.pipe', stream.getnames())
            for linkname in ('/etc/passwd', '/app/../etc/passwd'):
                with tarfile.open(root / 'unsafe.tar', 'w:') as stream:
                    item = tarfile.TarInfo('state/data/bad-link')
                    item.type = tarfile.SYMTYPE
                    item.linkname = linkname
                    stream.addfile(item)
                with self.assertRaisesRegex(RECOVERY.RecoveryError, 'archive_member_unsafe'):
                    RECOVERY.checked_members(root / 'unsafe.tar')


class RestoreStateTests(unittest.TestCase):
    """restore_state(): the data rollback for releases that change the schema."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.root = Path(self.temp.name)
        source = root / 'backup-source'
        (source / 'data').mkdir(parents=True)
        (source / 'release.json').write_text(json.dumps({'revision': 'a' * 40}))
        (source / 'data/upgrade-state.json').write_text(json.dumps({'commit': 'a' * 40}))
        with sqlite3.connect(source / 'data/v2.db') as db:
            db.execute('CREATE TABLE old_schema (id INTEGER)')
        (source / 'groups/main').mkdir(parents=True)
        (source / 'groups/main/notes.md').write_text('before the update')
        (source / 'signal').mkdir()
        (source / 'signal/ratchet').write_text('old signal state')
        env, onecli = root / 'env-source', root / 'onecli-source'
        env.write_text('EXAMPLE=value\n')
        onecli.mkdir()
        extras = {}
        for name in RECOVERY.PRIVATE_INPUTS.values():
            path = root / name.replace('/', '-')
            path.mkdir() if name.endswith('mail-downloads') else path.write_text('private')
            extras[name] = path
        self.backup = root / 'backups/one'
        self.backup.mkdir(parents=True, mode=0o700)
        os.chmod(root / 'backups', 0o700)
        keys = root / 'keys'
        keys.mkdir(mode=0o700)
        self.key = keys / 'one.key'
        self.key.write_text('e' * 64 + '\n')
        os.chmod(self.key, 0o600)
        archive, dump = root / 'state.tar', root / 'postgres.dump'
        dump.write_bytes(b'fixture-pg-dump')
        RECOVERY.archive_sources(archive, source, env, onecli, extras)
        payload = {'schema': RECOVERY.SCHEMA, 'revision': 'a' * 40,
                   'state': RECOVERY.encrypt(archive, self.backup / 'state.tar.enc', self.key),
                   'postgres': RECOVERY.encrypt(dump, self.backup / 'postgres.dump.enc', self.key)}
        payload['hmac_sha256'] = RECOVERY.manifest_mac(payload, self.key)
        (self.backup / 'manifest.json').write_text(json.dumps(payload))

        # The live state after a failed release: migrated DB, new agent work, newer Signal state.
        live = self.state = root / 'srv/nanoclaw'
        (live / 'data').mkdir(parents=True)
        with sqlite3.connect(live / 'data/v2.db') as db:
            db.execute('CREATE TABLE new_schema (id INTEGER)')
        (live / 'groups/main').mkdir(parents=True)
        (live / 'groups/main/notes.md').write_text('written by the failed release')
        (live / 'signal').mkdir()
        (live / 'signal/ratchet').write_text('current signal state')
        (live / 'proxy').mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def tables(self, path):
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
            return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def test_puts_back_data_and_groups_and_keeps_the_rest_and_a_copy(self):
        RECOVERY.restore_preflight(self.state, self.backup)
        output = io.StringIO()
        with redirect_stdout(output):
            RECOVERY.restore_state(self.state, self.backup, self.key)
        self.assertIn('state_restore=done', output.getvalue())
        self.assertEqual(self.tables(self.state / 'data/v2.db'), {'old_schema'})
        self.assertEqual((self.state / 'groups/main/notes.md').read_text(), 'before the update')
        # Signal's store and the proxy stay as they are.
        self.assertEqual((self.state / 'signal/ratchet').read_text(), 'current signal state')
        self.assertTrue((self.state / 'proxy').is_dir())
        aside = [path for path in self.state.parent.iterdir() if path.name.startswith('nanoclaw.failed-')]
        self.assertEqual(len(aside), 1)
        self.assertEqual(self.tables(aside[0] / 'data/v2.db'), {'new_schema'})
        self.assertEqual(sorted(path.name for path in aside[0].iterdir()), ['data', 'groups'])
        self.assertEqual(aside[0].stat().st_mode & 0o077, 0)
        leftovers = [path.name for path in self.state.parent.iterdir() if path.name.startswith('.nanoclaw-restore')]
        self.assertEqual(leftovers, [])
        self.assertEqual([path.name for path in self.backup.iterdir() if path.name.startswith('.recovery')], [])

    def test_a_failed_swap_moves_everything_back(self):
        real_rename = os.rename
        calls = []

        def flaky(source, destination):
            calls.append(destination)
            if len(calls) == 3:  # data swapped both ways, groups moved aside: then fail
                raise OSError('fixture failure')
            return real_rename(source, destination)

        with patch.object(RECOVERY.os, 'rename', side_effect=flaky), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                RECOVERY.restore_state(self.state, self.backup, self.key)
        self.assertEqual(self.tables(self.state / 'data/v2.db'), {'new_schema'})
        self.assertEqual((self.state / 'groups/main/notes.md').read_text(), 'written by the failed release')
        self.assertEqual([path.name for path in self.state.parent.iterdir()], ['nanoclaw'])

    def test_preflight_refuses_links_and_too_little_space(self):
        with patch.object(RECOVERY, 'free_bytes', return_value=1024):
            with self.assertRaisesRegex(RECOVERY.RecoveryError, 'restore_space_insufficient'):
                RECOVERY.restore_preflight(self.state, self.backup)
        (self.state / 'store').symlink_to(self.root)
        with self.assertRaisesRegex(RECOVERY.RecoveryError, 'restore_state_mount_unsupported'):
            RECOVERY.restore_preflight(self.state, self.backup)

    def test_a_wrong_key_changes_nothing(self):
        other = self.key.with_name('other.key')
        other.write_text('d' * 64 + '\n')
        os.chmod(other, 0o600)
        with self.assertRaisesRegex(RECOVERY.RecoveryError, 'backup_authentication_failed'):
            RECOVERY.restore_state(self.state, self.backup, other)
        self.assertEqual(self.tables(self.state / 'data/v2.db'), {'new_schema'})
        self.assertEqual(sorted(path.name for path in self.state.parent.iterdir()), ['nanoclaw'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
