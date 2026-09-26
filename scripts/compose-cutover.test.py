import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from argparse import Namespace
from contextlib import redirect_stdout
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name('compose-cutover.py')
SPEC = importlib.util.spec_from_file_location('compose_cutover', MODULE_PATH)
CUTOVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CUTOVER)


def staged_fixture(root):
    stage = root / 'stage'
    for name in CUTOVER.REQUIRED_DIRECTORIES:
        (stage / name).mkdir(parents=True, exist_ok=True)
    for name in CUTOVER.REQUIRED_FILES:
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name != 'state/data/v2.db':
            path.write_text('fixture')
    os.chmod(stage / 'env', 0o600)
    with sqlite3.connect(stage / 'state/data/v2.db') as db:
        db.execute('CREATE TABLE fixture (id INTEGER)')
    return stage


class ComposeCutoverTests(unittest.TestCase):
    def test_cli_has_no_apply_option_or_secret_echo(self):
        secret = 'fixture-private-value'
        result = subprocess.run([sys.executable, str(MODULE_PATH), '--apply', secret],
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('failure_category=invalid_arguments', result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_staged_tree_accepts_only_container_app_links(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = staged_fixture(Path(temp))
            (stage / 'state/data/skill').symlink_to('/app/skills/fixture')
            CUTOVER.valid_staged_members(stage)
            (stage / 'state/data/skill').unlink()
            (stage / 'state/data/skill').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(CUTOVER.RECOVERY.RecoveryError, 'stage_link_unsafe'):
                CUTOVER.valid_staged_members(stage)

    def test_staged_tree_rejects_hardlinks_and_missing_files(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = staged_fixture(Path(temp))
            os.link(stage / 'env', stage / 'duplicate-env')
            with self.assertRaisesRegex(CUTOVER.RECOVERY.RecoveryError, 'stage_hardlink_unsafe'):
                CUTOVER.valid_staged_members(stage)
            (stage / 'duplicate-env').unlink()
            (stage / 'env').unlink()
            with self.assertRaisesRegex(CUTOVER.RECOVERY.RecoveryError, 'stage_members_invalid'):
                CUTOVER.valid_staged_members(stage)

    def test_revision_requires_matching_active_and_staged_markers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'project'
            active = root / 'active'
            project.mkdir()
            (active / 'data').mkdir(parents=True)
            stage = staged_fixture(root)
            release = {'revision': 'a' * 40, 'tree': 'b' * 40}
            marker = {'commit': 'a' * 40}
            for path in (active / 'release.json', stage / 'state/release.json'):
                path.write_text(json.dumps(release))
            for path in (active / 'data/upgrade-state.json',
                         stage / 'state/data/upgrade-state.json'):
                path.write_text(json.dumps(marker))
            with patch.object(CUTOVER.RECOVERY, 'command', return_value=('a' * 40).encode()):
                CUTOVER.checked_revision(project, active, stage)
                (stage / 'state/data/upgrade-state.json').write_text(json.dumps({'commit': 'c' * 40}))
                with self.assertRaisesRegex(CUTOVER.RECOVERY.RecoveryError, 'release_marker_mismatch'):
                    CUTOVER.checked_revision(project, active, stage)

    def test_preflight_is_read_only_and_reports_fixed_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'project'
            active = root / 'active'
            project.mkdir()
            (active / 'data').mkdir(parents=True)
            stage = staged_fixture(root)
            os.chmod(stage, 0o700)
            (project / 'compose.yaml').write_text('services: {}\n')
            (project / '.env').write_text('FIXTURE=1\n')
            os.chmod(project / '.env', 0o600)
            (stage / 'env').write_text('FIXTURE=1\n')
            release = {'revision': 'a' * 40, 'tree': 'b' * 40}
            marker = {'commit': 'a' * 40}
            for path in (active / 'release.json', stage / 'state/release.json'):
                path.write_text(json.dumps(release))
            for path in (active / 'data/upgrade-state.json',
                         stage / 'state/data/upgrade-state.json'):
                path.write_text(json.dumps(marker))
            config = {'services': {'nanoclaw': {'volumes': [
                {'type': 'bind', 'target': '/srv/nanoclaw/data', 'source': str(active / 'data')},
                {'type': 'bind', 'target': '/srv/nanoclaw/release.json',
                 'source': str(active / 'release.json')}]}}}
            result = subprocess.CompletedProcess([], 0, b'TABLE fixture', b'')
            output = io.StringIO()
            with (patch.object(CUTOVER.RECOVERY, 'command', return_value=('a' * 40).encode()),
                  patch.object(CUTOVER.RECOVERY, 'compose', return_value=json.dumps(config).encode()) as compose,
                  patch.object(CUTOVER.RECOVERY, 'read_env_paths', return_value=('demo', {})),
                  patch.object(CUTOVER.RECOVERY, 'running_agents', return_value=[]),
                  patch.object(CUTOVER, 'volume_identity', side_effect=['onecli-volume', 'pg-volume']),
                  patch.object(CUTOVER.subprocess, 'run', return_value=result),
                  redirect_stdout(output)):
                CUTOVER.preflight(Namespace(project_root=str(project), state_root=str(active),
                                            stage_dir=str(stage)))
            self.assertEqual(output.getvalue(), 'cutover_preflight=ok\ncutover_mutation=disabled\n')
            compose.assert_called_once_with(project, 'config', '--format', 'json')


if __name__ == '__main__':
    unittest.main(verbosity=2)
