import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('ci-docker-build.sh')

FAKE_DOCKER = """#!/usr/bin/env bash
echo '#1 [internal] load build definition from Dockerfile'
echo '#5 [build 2/4] RUN corepack enable'
echo 'secret-looking-build-output-that-must-not-leak'
echo '#6 [build 3/4] RUN pnpm install --frozen-lockfile'
case "$FAKE_MODE" in
  ok) exit 0 ;;
  fail) exit 1 ;;
  hang) sleep 30 ;;
esac
"""


class CiDockerBuildTests(unittest.TestCase):
    def run_wrapper(self, mode, limit='30'):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'docker'
            fake.write_text(FAKE_DOCKER)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            env = {**os.environ, 'PATH': f'{tmp}:{os.environ["PATH"]}', 'RUNNER_TEMP': tmp,
                   'FAKE_MODE': mode, 'CI_DOCKER_BUILD_TIMEOUT': limit}
            result = subprocess.run(['bash', str(SCRIPT), 'host-review', '--', '-t', 'x', '.'],
                                    env=env, capture_output=True, text=True, timeout=60)
            full_log = (Path(tmp) / 'docker-build-host-review.log').read_text()
        return result, full_log

    def test_success_is_silent_and_keeps_the_full_log_on_disk(self):
        result, full_log = self.run_wrapper('ok')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('secret-looking-build-output', full_log)

    def test_failure_prints_only_step_headers(self):
        result, _ = self.run_wrapper('fail')
        self.assertEqual(result.returncode, 1)
        self.assertIn("docker build 'host-review' failed", result.stdout)
        self.assertIn('[build 3/4] RUN pnpm install --frozen-lockfile', result.stdout)
        self.assertNotIn('secret-looking-build-output', result.stdout)

    def test_hang_is_bounded_and_reported_as_timeout(self):
        result, _ = self.run_wrapper('hang', limit='1')
        self.assertIn(result.returncode, (124, 137))
        self.assertIn('timed out after 1s', result.stdout)
        self.assertIn('[build 3/4] RUN pnpm install', result.stdout)

    def test_rejects_unsafe_build_names(self):
        result = subprocess.run(['bash', str(SCRIPT), '../x', '--', '.'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
