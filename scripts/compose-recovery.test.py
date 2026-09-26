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

    def test_source_hardlink_fails_instead_of_silently_disappearing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state'
            state.mkdir()
            (state / 'first').write_text('fixture')
            os.link(state / 'first', state / 'second')
            with self.assertRaisesRegex(RECOVERY.RecoveryError, 'source_hardlink_unsupported'):
                RECOVERY.archive_sources(root / 'archive.tar', state, state / 'first',
                                         state, {name: state / 'first' for name in RECOVERY.PRIVATE_INPUTS.values()})

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
