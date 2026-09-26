import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name('compose-rehearsal.py')
SPEC = importlib.util.spec_from_file_location('compose_rehearsal', MODULE_PATH)
REHEARSAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REHEARSAL)


class ComposeRehearsalTests(unittest.TestCase):
    def test_requires_explicit_synthetic_apply_without_echoing_values(self):
        value = 'fixture-private-value'
        result = subprocess.run([sys.executable, str(MODULE_PATH), '--unexpected', value],
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('failure_category=invalid_arguments', result.stdout)
        self.assertNotIn(value, result.stdout + result.stderr)
        with self.assertRaisesRegex(REHEARSAL.RECOVERY.RecoveryError,
                                    'explicit_synthetic_confirmation_required'):
            REHEARSAL.rehearse(Namespace(apply=False, preflight=False,
                                        confirm_synthetic=True))

    def test_refuses_real_channel_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            env = Path(temp) / '.env'
            env.write_text('TELEGRAM_BOT_TOKEN=fixture-nonempty\n')
            with self.assertRaisesRegex(REHEARSAL.RECOVERY.RecoveryError,
                                        'real_channel_identity_present'):
                REHEARSAL.synthetic_config_only(env)

    def test_synthetic_state_requires_only_cli_messaging(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / 'data').mkdir()
            with sqlite3.connect(state / 'data/v2.db') as db:
                db.execute('CREATE TABLE messaging_groups (channel_type TEXT)')
                db.execute("INSERT INTO messaging_groups VALUES ('cli')")
                db.commit()
                REHEARSAL.synthetic_state_only(state)
                db.execute("INSERT INTO messaging_groups VALUES ('telegram')")
            with self.assertRaisesRegex(REHEARSAL.RECOVERY.RecoveryError,
                                        'non_cli_messaging_group_present'):
                REHEARSAL.synthetic_state_only(state)

    def run_transaction_fixture(self, fail_restored_prompt, *, preflight=False,
                                fail_restored_start=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'project'
            state = root / 'state'
            work_root = root / 'work'
            backup = root / 'backup'
            keys = root / 'keys'
            volume = root / 'volume'
            for path in (project, state, work_root, backup, keys, volume):
                path.mkdir(mode=0o700)
            (project / '.env').write_text('NANOCLAW_INSTALL_ID=fixture\n')
            (state / 'original-marker').write_text('original')
            key = keys / 'key'
            key.write_text('f' * 64)
            os.chmod(key, 0o600)
            args = Namespace(apply=not preflight, preflight=preflight,
                             confirm_synthetic=True,
                             project_root=str(project), state_root=str(state),
                             backup_dir=str(backup),
                             key_file=str(key), work_root=str(work_root))

            def stage_backup(options):
                target = Path(options.target_dir)
                (target / 'state').mkdir(parents=True)
                (target / 'state/restored-marker').write_text('restored')
                (target / 'onecli-data').mkdir()
                (target / 'postgres.dump').write_bytes(b'fixture-dump')

            def mocked_compose(_project, *items, override=None):
                if items[:2] == ('config', '--format'):
                    names = [line.split('name: ', 1)[1] for line in
                             override.read_text().splitlines() if 'name: ' in line]
                    return json.dumps({'volumes': {'onecli-data': {'name': names[0]},
                                                   'onecli-pgdata': {'name': names[1]}}}).encode()
                if (fail_restored_start and override is not None and
                        items == ('up', '-d', '--wait', '--force-recreate')):
                    REHEARSAL.RECOVERY.fail('command_failed')
                return b''

            def mocked_mount(_project, service, _destination, override=None):
                if override is None:
                    return ('old-onecli' if service == 'onecli' else 'old-pg', volume)
                names = [line.split('name: ', 1)[1] for line in
                         override.read_text().splitlines() if 'name: ' in line]
                return (names[0] if service == 'onecli' else names[1], volume)

            def mocked_run(command, **_kwargs):
                if command[:3] == ['docker', 'volume', 'inspect']:
                    return str(volume).encode()
                return b''

            def mocked_prompt(_state):
                if fail_restored_prompt and prompts[0] == 0:
                    prompts[0] += 1
                    REHEARSAL.RECOVERY.fail('synthetic_prompt_failed')
                prompts[0] += 1

            prompts = [0]
            output = io.StringIO()
            with (patch.object(REHEARSAL.CUTOVER, 'preflight') as cutover_preflight,
                  patch.object(REHEARSAL, 'synthetic_state_only'),
                  patch.object(REHEARSAL.RECOVERY, 'read_env_paths', return_value=('fixture', {})),
                  patch.object(REHEARSAL.RECOVERY, 'verify_or_stage', side_effect=stage_backup),
                  patch.object(REHEARSAL.RECOVERY, 'running_agents', return_value=[]),
                  patch.object(REHEARSAL, 'absent_volume'),
                  patch.object(REHEARSAL, 'mount_details', side_effect=mocked_mount),
                  patch.object(REHEARSAL, 'compose', side_effect=mocked_compose),
                  patch.object(REHEARSAL, 'run', side_effect=mocked_run),
                  patch.object(REHEARSAL, 'restore_postgres'),
                  patch.object(REHEARSAL, 'redacted_restored_status'),
                  patch.object(REHEARSAL, 'prompt_ready', side_effect=mocked_prompt),
                  redirect_stdout(output)):
                if fail_restored_prompt or fail_restored_start:
                    with self.assertRaisesRegex(REHEARSAL.RECOVERY.RecoveryError,
                                                'synthetic_prompt_failed' if fail_restored_prompt
                                                else 'command_failed'):
                        REHEARSAL.rehearse(args)
                else:
                    REHEARSAL.rehearse(args)
            cutover_preflight.assert_called_once()
            self.assertTrue(Path(cutover_preflight.call_args.args[0].stage_dir).is_dir())
            self.assertEqual((state / 'original-marker').read_text(), 'original')
            if preflight:
                self.assertEqual(prompts[0], 0)
                self.assertIn('rehearsal_runtime_mutation=disabled', output.getvalue())
                self.assertNotIn('original_stopped=yes', output.getvalue())
            else:
                self.assertEqual(prompts[0], 1 if fail_restored_start else 2)
                self.assertIn('original_rollback=healthy', output.getvalue())
                self.assertNotIn('original_rollback=failed_manual_recovery_needed', output.getvalue())
            if fail_restored_start:
                self.assertIn('restore_failed_phase=start_restored_stack', output.getvalue())
            if fail_restored_prompt or fail_restored_start or preflight:
                self.assertNotIn('recovery_rehearsal=ok', output.getvalue())
            else:
                self.assertIn('recovery_rehearsal=ok', output.getvalue())

    def test_rollback_after_restored_prompt_failure(self):
        self.run_transaction_fixture(True)

    def test_successful_rehearsal_still_rolls_back(self):
        self.run_transaction_fixture(False)

    def test_preflight_stages_but_does_not_stop_original(self):
        self.run_transaction_fixture(False, preflight=True)

    def test_rollback_after_restored_compose_failure(self):
        self.run_transaction_fixture(False, fail_restored_start=True)

    def test_service_status_redacts_unexpected_fields(self):
        raw = b'{"Service":"onecli","State":"running","Health":"unhealthy","Secret":"fixture-private-value"}\n'
        completed = subprocess.CompletedProcess([], 0, raw, b'')
        result = io.StringIO()
        with patch.object(REHEARSAL.subprocess, 'run', return_value=completed), redirect_stdout(result):
            REHEARSAL.redacted_restored_status(Path('/tmp'), Path('/tmp/override.yaml'))
        self.assertIn('restored_service_onecli=running/unhealthy', result.getvalue())
        self.assertNotIn('fixture-private-value', result.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
