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
                           {'NANOCLAW_INSTALL_ID': 'synthetic'})
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
