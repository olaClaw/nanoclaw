import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name('compose-release-update.py')
SPEC = importlib.util.spec_from_file_location('compose_release_update', MODULE_PATH)
UPDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATE)


def manifest(revision, tree, prefix):
    return {
        'schema': 'nanoclaw-compose-release/v1',
        'version': '2.4.0',
        'revision': revision,
        'tree': tree,
        'images': {name: f'ghcr.io/fixture/{prefix}-{name}@sha256:{"a" * 64}'
                   for name in UPDATE.IMAGE_KEYS},
    }


class ComposeReleaseUpdateTests(unittest.TestCase):
    def test_manifest_rejects_unpinned_images_extra_fields_and_bad_commit(self):
        good = manifest('a' * 40, 'b' * 40, 'old')
        self.assertEqual(UPDATE.validate_manifest(json.dumps(good).encode()), good)
        for broken in (
            {**good, 'unexpected': 'secret'},
            {**good, 'revision': 'short'},
            {**good, 'images': {**good['images'], 'host': 'ghcr.io/fixture:latest'}},
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(UPDATE.UpdateError):
                    UPDATE.validate_manifest(json.dumps(broken).encode())

    def test_env_rewrite_changes_only_fork_images_and_preserves_other_lines(self):
        source = (b'NANOCLAW_HOST_IMAGE=old\r\nNANOCLAW_AGENT_IMAGE=old\r\n'
                  b'NANOCLAW_BROKER_IMAGE=old\r\nPRIVATE_VALUE=unchanged\r\n')
        changed = UPDATE.rewrite_env(source, {
            'host': 'new-host', 'agent': 'new-agent', 'brokers': 'new-brokers'})
        self.assertIn(b'NANOCLAW_HOST_IMAGE=new-host\r\n', changed)
        self.assertIn(b'PRIVATE_VALUE=unchanged\r\n', changed)
        self.assertEqual(changed.count(b'PRIVATE_VALUE='), 1)
        with self.assertRaisesRegex(UPDATE.UpdateError, 'image_key_count_invalid'):
            UPDATE.rewrite_env(b'NANOCLAW_HOST_IMAGE=old\n', {
                'host': 'new-host', 'agent': 'new-agent', 'brokers': 'new-brokers'})
        with self.assertRaisesRegex(UPDATE.UpdateError, 'environment_key_invalid'):
            UPDATE.read_env(b'export SIGNAL_ACCOUNT=fixture-account\n')

    def test_atomic_write_preserves_owner_and_private_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'control'
            path.write_bytes(b'old')
            path.chmod(0o600)
            metadata = path.stat()
            UPDATE.atomic_write(path, b'new', metadata)
            self.assertEqual(path.read_bytes(), b'new')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_uid, metadata.st_uid)

    def test_synthetic_guard_rejects_real_channels_or_accounts(self):
        base = {'NANOCLAW_INSTALL_ID': 'synthetic'}
        UPDATE.require_synthetic(base, {'cli'})
        for values, channels in (
            ({**base, 'TELEGRAM_BOT_TOKEN': 'fixture-token'}, {'cli'}),
            ({**base, 'SIGNAL_ACCOUNT': 'fixture-account'}, {'cli'}),
            (base, {'cli', 'telegram'}),
            (base, set()),
        ):
            with self.subTest(channels=channels):
                with self.assertRaisesRegex(UPDATE.UpdateError,
                                            'synthetic_identity_required'):
                    UPDATE.require_synthetic(values, channels)

    def test_control_drift_is_rejected_before_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'env'
            path.write_bytes(b'original')
            UPDATE.controls_unchanged((path,), {path: b'original'})
            path.write_bytes(b'changed')
            with self.assertRaisesRegex(UPDATE.UpdateError,
                                        'control_files_changed_since_preflight'):
                UPDATE.controls_unchanged((path,), {path: b'original'})

    def test_apply_success_and_failed_host_start_rollback(self):
        for fail_host in (False, True):
            with self.subTest(fail_host=fail_host), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                project = root / 'project'
                state = root / 'state'
                project.mkdir()
                (state / 'data').mkdir(parents=True)
                controls = (project / '.env', state / 'release.json',
                            state / 'data/upgrade-state.json')
                old = manifest('a' * 40, 'b' * 40, 'old')
                new = manifest('c' * 40, 'd' * 40, 'new')
                # External images stay unchanged across a release update.
                for key in ('onecli', 'postgres', 'signal'):
                    new['images'][key] = old['images'][key]
                controls[0].write_text(''.join(
                    f'{key}={old["images"][name]}\n'
                    for name, key in UPDATE.IMAGE_KEYS.items()) +
                    'NANOCLAW_INSTALL_ID=synthetic\nPRIVATE_VALUE=unchanged\n')
                controls[1].write_text(json.dumps(old))
                controls[2].write_text(json.dumps({
                    'version': old['version'], 'commit': old['revision'],
                    'tree': old['tree']}))
                for path in controls:
                    path.chmod(0o600)
                old_bytes = {path: path.read_bytes() for path in controls}
                metadata = {path: path.stat() for path in controls}
                owner = project.stat()
                context = (project, controls, old_bytes, metadata, old,
                           json.dumps(new).encode(), new, owner,
                           {'NANOCLAW_INSTALL_ID': 'synthetic'}, controls[1].parent, False)
                head = [old['revision']]
                calls = []

                def fake_git(_project, *args, owner=None):
                    if args[:2] == ('switch', '--detach'):
                        head[0] = args[-1]
                    if args == ('rev-parse', 'HEAD^{tree}'):
                        return old['tree'] if head[0] == old['revision'] else new['tree']
                    if args == ('status', '--porcelain'):
                        return ''
                    return head[0]

                def fake_compose(_project, *args):
                    calls.append(args)
                    if (fail_host and args[0] == 'up' and
                            args[-1] == 'nanoclaw' and head[0] == new['revision']):
                        raise UPDATE.UpdateError('simulated_host_failure')
                    return ''

                output = io.StringIO()
                with (patch.object(UPDATE, 'git', side_effect=fake_git),
                      patch.object(UPDATE, 'compose', side_effect=fake_compose),
                      patch.object(UPDATE, 'no_agents'),
                      patch.object(UPDATE, 'service_health'),
                      redirect_stdout(output)):
                    if fail_host:
                        with self.assertRaisesRegex(UPDATE.UpdateError, 'update_failed'):
                            UPDATE.apply_update(context, root)
                    else:
                        UPDATE.apply_update(context, root)
                self.assertEqual(head[0], old['revision'] if fail_host else new['revision'])
                if fail_host:
                    self.assertTrue(all(path.read_bytes() == old_bytes[path]
                                        for path in controls))
                    self.assertIn('rollback=healthy', output.getvalue())
                else:
                    self.assertEqual(json.loads(controls[1].read_bytes()), new)
                    self.assertEqual(json.loads(controls[2].read_bytes())['commit'],
                                     new['revision'])
                    self.assertIn('PRIVATE_VALUE=unchanged', controls[0].read_text())
                    self.assertIn('release_update=healthy', output.getvalue())
                self.assertTrue(any(call[0] == 'up' and call[-1] == 'nanoclaw'
                                    for call in calls))
                backups = list(root.glob('control-*'))
                self.assertEqual(len(backups), 1)
                self.assertEqual((backups[0] / 'env.old').read_bytes(), old_bytes[controls[0]])

    def test_cli_never_echoes_private_argument(self):
        secret = 'fixture-private-value'
        result = subprocess.run([sys.executable, str(MODULE_PATH), '--unknown', secret],
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('failure_category=invalid_arguments', result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)


class ProductionModeTests(unittest.TestCase):
    def git_repo(self, root):
        env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}
        run = lambda *a: subprocess.run(['git', '-C', str(root), *a], env=env, check=True,
                                        capture_output=True, text=True).stdout.strip()
        run('init', '-q')
        return run

    def test_schema_fingerprint_ignores_tests_and_sees_migrations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.git_repo(root)
            (root / 'src/db/migrations').mkdir(parents=True)
            (root / 'src/modules/x/migrations').mkdir(parents=True)
            (root / 'src/modules/x/other.ts').write_text('a')
            (root / 'src/db/migrations/001-a.ts').write_text('a')
            (root / 'src/db/migrations/001-a.test.ts').write_text('t')
            (root / 'src/modules/x/migrations/m.ts').write_text('m')
            run('add', '-A'); run('commit', '-qm', 'one'); first = run('rev-parse', 'HEAD')
            (root / 'src/db/migrations/001-a.test.ts').write_text('t2')
            (root / 'src/modules/x/other.ts').write_text('b')
            run('commit', '-qam', 'two'); second = run('rev-parse', 'HEAD')
            (root / 'src/modules/x/migrations/m.ts').write_text('m2')
            run('commit', '-qam', 'three'); third = run('rev-parse', 'HEAD')
            fp = lambda rev: UPDATE.schema_fingerprint(root, rev, None)
            self.assertEqual(fp(first), fp(second))
            self.assertNotEqual(fp(second), fp(third))
            self.assertEqual({path for path, _ in fp(first)},
                             {'src/db/migrations/001-a.ts', 'src/modules/x/migrations/m.ts'})

    def test_backup_age_and_channel_readiness(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self.assertLess(UPDATE.backup_age_minutes({'created_utc': now.strftime('%Y%m%dT%H%M%SZ')}), 2)
        old = (now - timedelta(hours=3)).strftime('%Y%m%dT%H%M%SZ')
        self.assertGreater(UPDATE.backup_age_minutes({'created_utc': old}), 170)
        log = ('[10:00:00.000] \x1b[32mINFO\x1b[39m \x1b[36mChannel adapter started\x1b[39m channel="cli"\n'
               '[10:00:00.100] INFO Channel adapter started channel="signal"\n'
               '[10:00:00.200] INFO Signal channel connected account="+0"\n'
               '[10:00:00.300] WARN Channel credentials missing, skipping channel="x"\n')
        titles = UPDATE.log_titles(log)
        self.assertIn('Signal channel connected', titles)
        self.assertNotIn('+0', ' '.join(titles))
        self.assertTrue(UPDATE.channels_ready(titles, {'SIGNAL_ACCOUNT': '+0'}))
        self.assertFalse(UPDATE.channels_ready(titles, {'SIGNAL_ACCOUNT': '+0', 'TELEGRAM_BOT_TOKEN': 't'}))
        self.assertFalse(UPDATE.channels_ready(titles[:2], {'SIGNAL_ACCOUNT': '+0'}))

    def test_production_apply_refreshes_images_and_rolls_back_when_that_fails(self):
        for fail_refresh in (False, True):
            with self.subTest(fail_refresh=fail_refresh), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                project, state = root / 'project', root / 'state'
                (state / 'data').mkdir(parents=True)
                project.mkdir()
                old = manifest('a' * 40, 'b' * 40, 'old')
                new = manifest('c' * 40, 'd' * 40, 'new')
                for name in ('onecli', 'postgres', 'signal'):
                    new['images'][name] = old['images'][name]
                controls = (project / '.env', state / 'release.json', state / 'data/upgrade-state.json')
                env = ''.join(f'{key}={old["images"][name]}\n' for name, key in UPDATE.IMAGE_KEYS.items())
                controls[0].write_text(env + 'NANOCLAW_INSTALL_ID=prod\nSIGNAL_ACCOUNT=+0\n')
                controls[1].write_text(json.dumps(old))
                controls[2].write_text(json.dumps({'version': '2.4.0', 'commit': old['revision'],
                                                   'tree': old['tree']}))
                for path in controls:
                    os.chmod(path, 0o600)
                old_bytes = {path: path.read_bytes() for path in controls}
                metadata = {path: path.stat() for path in controls}
                context = (project, controls, old_bytes, metadata, old, json.dumps(new).encode(), new,
                           project.stat(), {'NANOCLAW_INSTALL_ID': 'prod', 'SIGNAL_ACCOUNT': '+0'}, state, True)
                head = [old['revision']]
                events = []

                def fake_git(_project, *args, owner=None):
                    if args[:2] == ('switch', '--detach'):
                        head[0] = args[-1]
                    if args == ('rev-parse', 'HEAD^{tree}'):
                        return old['tree'] if head[0] == old['revision'] else new['tree']
                    if args == ('status', '--porcelain'):
                        return ''
                    return head[0]

                def refresh(*_):
                    events.append('refresh')
                    if fail_refresh:
                        raise UPDATE.UpdateError('derived_image_not_refreshed')

                output = io.StringIO()
                with (patch.object(UPDATE, 'git', side_effect=fake_git),
                      patch.object(UPDATE, 'compose', return_value=''),
                      patch.object(UPDATE, 'stop_agents', side_effect=lambda *_: events.append('stop_agents')),
                      patch.object(UPDATE, 'wait_for_channels', side_effect=lambda *_: events.append('channels')),
                      patch.object(UPDATE, 'refresh_derived_images', side_effect=refresh),
                      patch.object(UPDATE, 'service_health'),
                      redirect_stdout(output)):
                    if fail_refresh:
                        with self.assertRaisesRegex(UPDATE.UpdateError, 'update_failed'):
                            UPDATE.apply_update(context, root)
                    else:
                        UPDATE.apply_update(context, root)
                self.assertEqual(events[:3], ['stop_agents', 'channels', 'refresh'])
                if fail_refresh:
                    self.assertEqual(head[0], old['revision'])
                    self.assertTrue(all(path.read_bytes() == old_bytes[path] for path in controls))
                    self.assertIn('rollback=healthy', output.getvalue())
                else:
                    self.assertEqual(head[0], new['revision'])
                    self.assertIn('release_update=healthy', output.getvalue())


class DerivedImageRefreshTests(unittest.TestCase):
    """The update waits for the host's own rebuild instead of starting a concurrent one."""

    def run_refresh(self, label_sequence, host_log, *, wait=30, grace=0):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / 'data').mkdir()
            with sqlite3.connect(state / 'data/v2.db') as db:
                db.execute('CREATE TABLE container_configs (agent_group_id TEXT, image_tag TEXT, '
                           'packages_apt TEXT, packages_npm TEXT)')
                db.execute("INSERT INTO container_configs VALUES ('ag-1', 'base:ag-1', '[\"ffmpeg\"]', '[]')")
                db.execute("INSERT INTO container_configs VALUES ('ag-2', NULL, '[]', '[]')")
            target = manifest('c' * 40, 'd' * 40, 'new')
            labels = iter(label_sequence)
            exec_calls = []

            def fake_command(argv, **_):
                if argv[:3] == ['docker', 'image', 'inspect'] and '{{.Id}}' in argv:
                    return 'sha256:base'
                if argv[:2] == ['docker', 'logs']:
                    return host_log
                return ''

            def fake_compose(_project, *args, **_):
                if args[:1] == ('exec',):
                    exec_calls.append(args)
                return 'host-container'

            output = io.StringIO()
            with (patch.object(UPDATE, 'command', side_effect=fake_command),
                  patch.object(UPDATE, 'compose', side_effect=fake_compose),
                  patch.object(UPDATE, 'image_labels', side_effect=lambda _tag: next(labels)),
                  patch.object(UPDATE.time, 'sleep'),
                  redirect_stdout(output)):
                try:
                    UPDATE.refresh_derived_images(Path(tmp), state, target, '2026-09-27T00:00:00Z',
                                                  wait=wait, grace=grace, poll=0)
                    error = None
                except UPDATE.UpdateError as exc:
                    error = exc
            return exec_calls, output.getvalue(), error

    current = {'dev.nanoclaw.derived-from': 'sha256:base', 'org.opencontainers.image.revision': 'c' * 40}
    stale = {'dev.nanoclaw.derived-from': 'sha256:old', 'org.opencontainers.image.revision': 'a' * 40}

    def test_host_rebuilding_by_itself_is_awaited_not_duplicated(self):
        log = 'INFO Rebuilding per-agent-group image on current base agentGroupId="ag-1"\n'
        calls, output, error = self.run_refresh([self.stale, self.stale, self.current], log)
        self.assertIsNone(error)
        self.assertEqual(calls, [])
        self.assertIn('rebuilds_triggered=0', output)

    def test_idle_host_gets_exactly_one_rebuild(self):
        calls, output, error = self.run_refresh([self.stale, self.stale, self.stale, self.current],
                                                'INFO NanoClaw running\n')
        self.assertIsNone(error)
        self.assertEqual(len(calls), 1)
        self.assertIn('--rebuild', calls[0])
        self.assertIn('rebuilds_triggered=1', output)

    def test_image_that_never_becomes_current_fails(self):
        calls, _, error = self.run_refresh([self.stale] * 5 + [{}] * 50, 'INFO NanoClaw running\n', wait=0)
        self.assertIsNotNone(error)
        self.assertIn('derived_image_not_refreshed', str(error))


if __name__ == '__main__':
    unittest.main(verbosity=2)
