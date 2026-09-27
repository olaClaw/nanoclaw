import http.client
import importlib.util
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name('compose-ops.py')
SPEC = importlib.util.spec_from_file_location('compose_ops', MODULE_PATH)
OPS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OPS)

KEY = bytes([7]) * 32
OLD, NEW = 'a' * 40, 'c' * 40


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    os.chmod(path, 0o600)


def fixture(root, backups=2):
    state, control, backup, candidates = (root / name for name in ('state', 'control', 'backups', 'candidates'))
    private(state / 'release.json', {'schema': 'nanoclaw-compose-release/v1', 'version': '2.4.0', 'revision': NEW,
                                     'tree': 'd' * 40, 'images': {}})
    job = {'schema': 'nanoclaw-compose-release-job/v1', 'job_id': '0123456789abcdef', 'mode': 'production',
           'apply': True, 'from_revision': OLD, 'to_revision': NEW, 'to_version': '2.4.0',
           'phase': 'refresh_dashboard', 'outcome': 'succeeded', 'failure_category': None, 'rollback': None,
           'started_utc': '2026-01-15T12:00:00Z', 'updated_utc': '2026-01-15T12:04:00Z',
           'finished_utc': '2026-01-15T12:04:00Z',
           'phases': [{'phase': 'preflight', 'at': '2026-01-15T12:00:00Z'},
                      {'phase': 'unknown_phase', 'at': '2026-01-15T12:00:01Z'}]}
    private(control / OPS.JOB_FILE, job)
    older = {**job, 'job_id': 'fedcba9876543210', 'outcome': 'rolled_back', 'rollback': 'healthy',
             'failure_category': 'command_failed', 'phases': None}
    private(control / OPS.JOB_HISTORY, json.dumps(older) + '\nnot json\n' + json.dumps(job) + '\n')
    for index in range(backups):
        folder = backup / f'{OLD[:8]}-2026011{index % 10}T12000{index % 10}Z-{index:06x}'
        private(folder / 'manifest.json', {'schema': 'nanoclaw-compose-backup/v1', 'revision': OLD,
                                            'created_utc': f'2026011{index % 10}T12000{index % 10}Z',
                                            'hmac_sha256': 'x'})
        private(folder / 'state.tar.enc', 'x' * 100)
    private(candidates / 'next.json', {'version': '2.4.1', 'revision': 'e' * 40})
    return OPS.Sources(state, backup, control, candidates, KEY)


class HandleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_releases_report_installed_candidate_and_last_update(self):
        status, body = OPS.handle(fixture(self.root), 'GET', '/api/v1/releases')
        self.assertEqual(status, 200)
        self.assertEqual(body['installed'], {'version': '2.4.0', 'revision': NEW})
        self.assertEqual(body['candidate'], {'version': '2.4.1', 'revision': 'e' * 40, 'verified': True})
        job = body['last_update']
        self.assertEqual((job['id'], job['kind'], job['outcome'], job['phase']),
                         ('job_0123456789abcdef', 'update', 'succeeded', 'refresh_dashboard'))
        self.assertEqual(job['phases'], [{'phase': 'preflight', 'at': '2026-01-15T12:00:00Z'}])

    def test_backups_carry_opaque_ids_and_no_paths(self):
        sources = fixture(self.root)
        status, body = OPS.handle(sources, 'GET', '/api/v1/backups')
        self.assertEqual(status, 200)
        self.assertEqual(len(body['items']), 2)
        text = json.dumps(body)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(OLD[:8] + '-', text)
        for item in body['items']:
            self.assertRegex(item['id'], r'^bkp_[0-9a-f]{32}$')
            self.assertEqual((item['verification'], item['exportable']), ('verified', False))
            self.assertGreater(item['size_bytes'], 100)
        self.assertGreater(body['items'][0]['created_at'], body['items'][1]['created_at'])

    def test_backup_ids_match_the_dashboard_derivation(self):
        import hashlib
        import hmac
        name = 'folder-name'
        expected = 'bkp_' + hmac.new(KEY, b'backup\0' + name.encode(), hashlib.sha256).hexdigest()[:32]
        self.assertEqual(fixture(self.root, 0).public_id('backup', name), expected)

    def test_backups_page_with_a_cursor(self):
        sources = fixture(self.root, OPS.PAGE_SIZE + 3)
        first = OPS.handle(sources, 'GET', '/api/v1/backups')[1]
        self.assertEqual((len(first['items']), first['next_cursor']), (OPS.PAGE_SIZE, f'o{OPS.PAGE_SIZE}'))
        second = OPS.handle(sources, 'GET', f'/api/v1/backups?cursor={first["next_cursor"]}')[1]
        self.assertEqual((len(second['items']), second['next_cursor']), (3, None))

    def test_jobs_are_found_by_id_including_history(self):
        sources = fixture(self.root)
        self.assertEqual(OPS.handle(sources, 'GET', '/api/v1/jobs/job_fedcba9876543210')[1]['rollback'], 'healthy')
        for target in ('/api/v1/jobs/job_0000000000000000', '/api/v1/jobs/..%2f', '/api/v1/jobs/0123456789abcdef'):
            with self.subTest(target=target), self.assertRaises(OPS.OpsError) as caught:
                OPS.handle(sources, 'GET', target)
            self.assertEqual(caught.exception.code, 'not_found')

    def test_refuses_other_paths_methods_and_queries(self):
        sources = fixture(self.root)
        cases = [('GET', '/api/v1/agents', 'not_found'), ('POST', '/api/v1/releases', 'method_not_allowed'),
                 ('POST', '/api/v1/updates', 'invalid_request'), ('POST', '/api/v1/backups/bkp_x/verify', 'not_implemented'),
                 ('GET', '/api/v1/releases?cursor=o50', 'invalid_query'), ('GET', '/api/v1/backups?limit=5', 'invalid_query'),
                 ('GET', '/api/v1/backups?cursor=%27', 'invalid_query'), ('GET', 'relative', 'invalid_request')]
        for method, target, code in cases:
            with self.subTest(target=target), self.assertRaises(OPS.OpsError) as caught:
                OPS.handle(sources, method, target)
            self.assertEqual(caught.exception.code, code)

    def test_unsafe_or_malformed_files_are_ignored(self):
        sources = fixture(self.root)
        os.chmod(self.root / 'state/release.json', 0o666)
        with self.assertRaises(OPS.OpsError) as caught:
            OPS.handle(sources, 'GET', '/api/v1/releases')
        self.assertEqual(caught.exception.code, 'release_unknown')
        private(self.root / 'control' / OPS.JOB_FILE, {'job_id': 'nothex', 'started_utc': 'x'})
        os.chmod(self.root / 'state/release.json', 0o600)
        body = OPS.handle(sources, 'GET', '/api/v1/releases')[1]
        # The broken current record is skipped; the newest history line wins.
        self.assertEqual(body['last_update']['id'], 'job_0123456789abcdef')


class SocketTests(unittest.TestCase):
    def test_serves_json_with_no_store_over_the_socket(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = str(root / 'ops.sock')
            server = OPS.UnixServer(path, OPS.make_handler(fixture(root)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def call(method, target, body=None):
                    connection = http.client.HTTPConnection('ops')
                    connection.sock = socket.socket(socket.AF_UNIX)
                    connection.sock.connect(path)
                    connection.request(method, target, body=body,
                                       headers={'Content-Type': 'application/json'} if body else {})
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), json.loads(response.read())

                status, headers, body = call('GET', '/api/v1/releases')
                self.assertEqual((status, headers['Cache-Control']), (200, 'no-store'))
                status, _, body = call('POST', '/api/v1/updates', json.dumps({'release_revision': 'e' * 40, 'confirm': True}).encode())
                self.assertEqual((status, body['error']['code']), (501, 'not_implemented'))
                self.assertRegex(body['error']['request_id'], r'^req_[0-9a-f]{16}$')
            finally:
                server.shutdown()
                server.server_close()


FAKE_RECOVERY = """
import os, sys, secrets
args = dict(zip(sys.argv[2::2], sys.argv[3::2]))
if os.environ.get('FAKE_BACKUP_FAIL'):
    print('backup_preflight=ok'); print('failure_category=postgres_dump_empty'); sys.exit(2)
name = 'aaaaaaaa-20260115T130000Z-' + secrets.token_hex(3)
folder = os.path.join(args['--backup-root'], name); os.mkdir(folder, 0o700)
open(os.path.join(folder, 'manifest.json'), 'w').write('{"revision": "' + 'a' * 40 + '", "created_utc": "20260115T130000Z", "hmac_sha256": "x"}')
os.chmod(os.path.join(folder, 'manifest.json'), 0o600)
key = os.path.join(args['--key-root'], name + '.key'); open(key, 'w').write(secrets.token_hex(32) + '\\n'); os.chmod(key, 0o600)
print('backup=verified')
"""


class BackupOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.root = Path(self.temp.name)
        base = fixture(root, 0)
        for name in ('keys', 'jobs', 'project'):
            (root / name).mkdir(mode=0o700)
        script = root / 'compose-recovery.py'
        script.write_text(FAKE_RECOVERY)
        self.sources = OPS.Sources(base.state_root, base.backup_root, base.control_root, None, KEY,
                                   key_root=root / 'keys', project_root=root / 'project',
                                   jobs_dir=root / 'jobs', recovery_script=script)
        (base.backup_root).mkdir(exist_ok=True)

    def tearDown(self):
        os.environ.pop('FAKE_BACKUP_FAIL', None)
        self.temp.cleanup()

    def wait(self, job_id):
        for _ in range(100):
            job = OPS.handle(self.sources, 'GET', f'/api/v1/jobs/{job_id}')[1]
            if job['outcome'] != 'running':
                return job
            threading.Event().wait(0.05)
        self.fail('job did not finish')

    def test_backup_job_then_key_shown_once_and_shredded(self):
        status, accepted = OPS.handle(self.sources, 'POST', '/api/v1/backups', {'confirm': True})
        self.assertEqual((status, accepted['job']['kind']), (202, 'backup_create'))
        job = self.wait(accepted['job']['id'])
        self.assertEqual((job['outcome'], job['phase'], job['failure_category']), ('succeeded', 'done', None))
        backup = job['backup']
        self.assertRegex(backup, r'^bkp_[0-9a-f]{32}$')
        listed = OPS.handle(self.sources, 'GET', '/api/v1/backups')[1]['items']
        self.assertEqual([(item['id'], item['key_on_host']) for item in listed], [(backup, True)])

        shown = OPS.handle(self.sources, 'POST', f'/api/v1/backups/{backup}/key', {'confirm': True})[1]
        self.assertRegex(shown['key'], r'^[0-9a-f]{64}$')
        key_file = next((self.root / 'keys').iterdir())
        self.assertEqual(key_file.read_text().strip(), shown['key'])

        saved = OPS.handle(self.sources, 'POST', f'/api/v1/backups/{backup}/key/saved', {'confirm': True})[1]
        self.assertEqual(saved, {'backup': backup, 'key_on_host': False})
        self.assertFalse(key_file.exists())
        with self.assertRaises(OPS.OpsError) as caught:
            OPS.handle(self.sources, 'POST', f'/api/v1/backups/{backup}/key', {'confirm': True})
        self.assertEqual(caught.exception.code, 'key_not_on_host')
        self.assertFalse(OPS.handle(self.sources, 'GET', '/api/v1/backups')[1]['items'][0]['key_on_host'])

    def test_failed_backup_reports_the_tool_category(self):
        os.environ['FAKE_BACKUP_FAIL'] = '1'
        job_id = OPS.handle(self.sources, 'POST', '/api/v1/backups', {'confirm': True})[1]['job']['id']
        job = self.wait(job_id)
        self.assertEqual((job['outcome'], job['failure_category'], job['backup']), ('failed', 'postgres_dump_empty', None))

    def test_refuses_while_the_update_lock_is_held(self):
        fd = os.open(self.sources.control_root / OPS.LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            OPS.fcntl.flock(fd, OPS.fcntl.LOCK_EX)
            with self.assertRaises(OPS.OpsError) as caught:
                OPS.handle(self.sources, 'POST', '/api/v1/backups', {'confirm': True})
            self.assertEqual((caught.exception.status, caught.exception.code), (409, 'operation_in_progress'))
        finally:
            os.close(fd)
        self.assertEqual(list((self.root / 'jobs').iterdir()), [])

    def test_bodies_ids_and_methods_are_checked(self):
        for body in (None, {}, {'confirm': False}, {'confirm': True, 'extra': 1}, [True]):
            with self.subTest(body=body), self.assertRaises(OPS.OpsError) as caught:
                OPS.handle(self.sources, 'POST', '/api/v1/backups', body)
            self.assertEqual(caught.exception.code, 'invalid_request')
        for target in ('/api/v1/backups/bkp_' + '0' * 32 + '/key', '/api/v1/backups/../key'):
            with self.subTest(target=target), self.assertRaises(OPS.OpsError) as caught:
                OPS.handle(self.sources, 'POST', target, {'confirm': True})
            self.assertEqual(caught.exception.code, 'not_found')
        with self.assertRaises(OPS.OpsError) as caught:
            OPS.handle(self.sources, 'GET', '/api/v1/backups/bkp_' + '0' * 32 + '/key')
        self.assertEqual(caught.exception.code, 'method_not_allowed')

    def test_linked_roots_reach_the_backup_tool_as_real_paths(self):
        links = self.root / 'links'
        links.mkdir()
        for name, target in (('backups', self.sources.backup_root), ('keys', self.root / 'keys'),
                             ('project', self.root / 'project'), ('control', self.sources.control_root)):
            (links / name).symlink_to(target)
        linked = OPS.Sources(self.sources.state_root, links / 'backups', links / 'control', None, KEY,
                             key_root=links / 'keys', project_root=links / 'project',
                             jobs_dir=self.root / 'jobs', recovery_script=self.sources.recovery_script)
        for path in (linked.backup_root, linked.key_root, linked.project_root, linked.control_root):
            self.assertFalse(path.is_symlink())
        job_id = OPS.handle(linked, 'POST', '/api/v1/backups', {'confirm': True})[1]['job']['id']
        self.sources = linked
        self.assertEqual(self.wait(job_id)['outcome'], 'succeeded')

    def test_without_operation_paths_backups_stay_not_implemented(self):
        read_only = OPS.Sources(self.sources.state_root, self.sources.backup_root, self.sources.control_root, None, KEY)
        with self.assertRaises(OPS.OpsError) as caught:
            OPS.handle(read_only, 'POST', '/api/v1/backups', {'confirm': True})
        self.assertEqual(caught.exception.code, 'not_implemented')


class CandidateFetchTests(unittest.TestCase):
    def setUp(self):
        import subprocess
        self.temp = tempfile.TemporaryDirectory()
        root = self.root = Path(self.temp.name)
        run = lambda *args, cwd=None: subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        env = ['-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
        run('git', 'init', '-q', '--bare', '-b', 'main', str(root / 'origin.git'))
        run('git', 'clone', '-q', str(root / 'origin.git'), str(root / 'work'))
        commit = lambda message: (run('git', *env, 'commit', '-q', '--allow-empty', '-m', message, cwd=root / 'work'),
                                  run('git', 'rev-parse', 'HEAD', cwd=root / 'work'))[1]
        self.installed = commit('installed')
        self.next = commit('next')
        run('git', 'push', '-q', 'origin', 'main', cwd=root / 'work')
        run('git', 'checkout', '-q', '--orphan', 'other', cwd=root / 'work')
        self.unrelated = commit('unrelated')
        run('git', 'clone', '-q', str(root / 'origin.git'), str(root / 'project'))
        run('git', '-C', str(root / 'project'), 'reset', '-q', '--hard', self.installed)
        run('git', 'push', '-q', 'origin', 'other', cwd=root / 'work')
        run('git', '-C', str(root / 'project'), 'fetch', '-q', 'origin', 'other')
        private(root / 'state/release.json', {'version': '2.4.0', 'revision': self.installed})
        (root / 'candidates').mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, revision, **changes):
        images = {key: f'ghcr.io/fixture/{key}@sha256:' + 'a' * 64 for key in OPS.IMAGE_KEYS}
        return {'schema': 'nanoclaw-compose-release/v1', 'version': '2.4.0', 'revision': revision,
                'tree': 'b' * 40, 'images': images, **changes}

    def fetch(self, releases, assets):
        def get(url, _limit):
            if url.startswith('https://api.github.com/'):
                return json.dumps(releases).encode()
            return json.dumps(assets[url]).encode() if isinstance(assets[url], dict) else assets[url]
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = OPS.fetch_candidates('olaClaw/nanoclaw', self.root / 'candidates', self.root / 'state',
                                        self.root / 'project', get=get)
        return code, out.getvalue(), sorted(p.name for p in (self.root / 'candidates').iterdir())

    def release(self, revision, name='compose-release.json', tag=None):
        url = f'https://github.com/olaClaw/nanoclaw/releases/download/{revision[:8]}/{name}'
        return {'tag_name': tag or f'compose-{revision[:8]}', 'draft': False, 'prerelease': False,
                'assets': [{'name': name, 'browser_download_url': url}]}, url

    def test_stores_a_descendant_and_skips_unrelated_invalid_and_mislabelled(self):
        good, good_url = self.release(self.next)
        other, other_url = self.release(self.unrelated)
        broken, broken_url = self.release('c' * 40)
        wrong_tag, wrong_url = self.release(self.next, tag='compose-deadbeef')
        installed, installed_url = self.release(self.installed)
        code, output, files = self.fetch(
            [other, broken, wrong_tag, good, installed],
            {good_url: self.manifest(self.next), other_url: self.manifest(self.unrelated),
             broken_url: b'{not json', wrong_url: self.manifest(self.next), installed_url: self.manifest(self.installed)})
        self.assertEqual((code, output.strip(), files), (0, 'candidates=1', [f'{self.next}.json']))
        sources = OPS.Sources(self.root / 'state', self.root / 'backups', self.root / 'control', self.root / 'candidates', KEY)
        self.assertEqual(sources.candidate({'revision': self.installed})['revision'], self.next)

    def test_refuses_unpinned_images_and_foreign_download_hosts(self):
        release, url = self.release(self.next)
        unpinned = self.manifest(self.next, images={key: 'ghcr.io/fixture/x:latest' for key in OPS.IMAGE_KEYS})
        self.assertEqual(self.fetch([release], {url: unpinned})[2], [])
        release['assets'][0]['browser_download_url'] = 'https://evil.example.invalid/compose-release.json'
        self.assertEqual(self.fetch([release], {})[2], [])

    def test_removes_candidates_that_are_no_longer_offered(self):
        stale = self.root / 'candidates' / ('d' * 40 + '.json')
        private(stale, self.manifest('d' * 40))
        self.assertEqual(self.fetch([], {})[2], [])


FAKE_UPDATE = """
import os, sys
if os.environ.get('FAKE_UPDATE_RESULT') == 'rolled_back':
    print('release_update=failed'); print('failure_category=channels_not_ready'); print('rollback=healthy'); sys.exit(2)
assert '--confirm-synthetic' in sys.argv and '--apply' in sys.argv
print('release_update=healthy')
"""


class UpdateOperationTests(BackupOperationTests):
    def setUp(self):
        super().setUp()
        root = self.root
        (root / 'candidates').mkdir(mode=0o700, exist_ok=True)
        self.revision = 'e' * 40
        images = {key: f'ghcr.io/fixture/{key}@sha256:' + 'a' * 64 for key in OPS.IMAGE_KEYS}
        private(root / 'candidates' / f'{self.revision}.json',
                {'schema': 'nanoclaw-compose-release/v1', 'version': '2.4.1', 'revision': self.revision,
                 'tree': 'f' * 40, 'images': images})
        (root / 'compose-release-update.py').write_text(FAKE_UPDATE)
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        docker = bin_dir / 'docker'
        docker.write_text('#!/bin/sh\n[ -n "$FAKE_PULL_FAIL" ] && exit 1\nexit 0\n')
        docker.chmod(0o755)
        self.path = os.environ['PATH']
        os.environ['PATH'] = f'{bin_dir}:{self.path}'
        s = self.sources
        self.sources = OPS.Sources(s.state_root, s.backup_root, s.control_root, root / 'candidates', KEY,
                                   key_root=s.key_root, project_root=s.project_root, jobs_dir=s.jobs_dir,
                                   recovery_script=s.recovery_script, update_script=root / 'compose-release-update.py',
                                   update_mode='synthetic')

    def tearDown(self):
        os.environ['PATH'] = self.path
        for name in ('FAKE_UPDATE_RESULT', 'FAKE_PULL_FAIL'):
            os.environ.pop(name, None)
        super().tearDown()

    def start(self):
        status, body = OPS.handle(self.sources, 'POST', '/api/v1/updates',
                                  {'release_revision': self.revision, 'confirm': True})
        self.assertEqual((status, body['job']['kind']), (202, 'update'))
        return self.wait(body['job']['id'])

    def test_update_pulls_backs_up_and_applies_then_offers_the_key(self):
        job = self.start()
        self.assertEqual((job['kind'], job['outcome'], job['failure_category']), ('update', 'succeeded', None))
        self.assertEqual([p['phase'] for p in job['phases']], ['pull', 'backup', 'update', 'done'])
        self.assertEqual(job['release']['to_revision'], self.revision)
        self.assertRegex(job['backup'], r'^bkp_[0-9a-f]{32}$')
        key = OPS.handle(self.sources, 'POST', f'/api/v1/backups/{job["backup"]}/key', {'confirm': True})[1]
        self.assertRegex(key['key'], r'^[0-9a-f]{64}$')

    def test_rollback_and_failures_are_reported(self):
        os.environ['FAKE_UPDATE_RESULT'] = 'rolled_back'
        job = self.start()
        self.assertEqual((job['outcome'], job['failure_category'], job['rollback']),
                         ('rolled_back', 'channels_not_ready', 'healthy'))
        os.environ.pop('FAKE_UPDATE_RESULT')
        os.environ['FAKE_PULL_FAIL'] = '1'
        job = self.start()
        self.assertEqual((job['outcome'], job['failure_category'], job['backup']), ('failed', 'image_pull_failed', None))

    def test_only_a_stored_candidate_can_be_installed(self):
        for body, code in (({'release_revision': 'd' * 40, 'confirm': True}, 'candidate_unknown'),
                           ({'release_revision': self.revision}, 'invalid_request'),
                           ({'release_revision': 'short', 'confirm': True}, 'invalid_request')):
            with self.subTest(body=body), self.assertRaises(OPS.OpsError) as caught:
                OPS.handle(self.sources, 'POST', '/api/v1/updates', body)
            self.assertEqual(caught.exception.code, code)

    def test_update_is_refused_while_the_lock_is_held(self):
        fd = os.open(self.sources.control_root / OPS.LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            OPS.fcntl.flock(fd, OPS.fcntl.LOCK_EX)
            with self.assertRaises(OPS.OpsError) as caught:
                OPS.handle(self.sources, 'POST', '/api/v1/updates', {'release_revision': self.revision, 'confirm': True})
            self.assertEqual(caught.exception.code, 'operation_in_progress')
        finally:
            os.close(fd)


class BackupDeleteTests(BackupOperationTests):
    def make(self, count):
        ids = []
        for _ in range(count):
            job_id = OPS.handle(self.sources, 'POST', '/api/v1/backups', {'confirm': True})[1]['job']['id']
            ids.append(self.wait(job_id)['backup'])
        return ids

    def test_deletes_archive_and_key_but_never_the_last_backup(self):
        first, second = self.make(2)
        self.assertEqual(len(list((self.root / 'keys').iterdir())), 2)
        body = OPS.handle(self.sources, 'POST', f'/api/v1/backups/{first}/delete', {'confirm': True})[1]
        self.assertEqual(body, {'backup': first, 'deleted': True})
        listed = [item['id'] for item in OPS.handle(self.sources, 'GET', '/api/v1/backups')[1]['items']]
        self.assertEqual(listed, [second])
        self.assertEqual(len(list((self.root / 'keys').iterdir())), 1)
        with self.assertRaises(OPS.OpsError) as caught:
            OPS.handle(self.sources, 'POST', f'/api/v1/backups/{second}/delete', {'confirm': True})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, 'last_backup'))

    def test_refuses_while_an_operation_holds_the_lock_and_checks_the_request(self):
        first, _ = self.make(2)
        fd = os.open(self.sources.control_root / OPS.LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            OPS.fcntl.flock(fd, OPS.fcntl.LOCK_EX)
            with self.assertRaises(OPS.OpsError) as caught:
                OPS.handle(self.sources, 'POST', f'/api/v1/backups/{first}/delete', {'confirm': True})
            self.assertEqual(caught.exception.code, 'operation_in_progress')
        finally:
            os.close(fd)
        for method, target, body, code in (
                ('POST', f'/api/v1/backups/{first}/delete', {'confirm': False}, 'invalid_request'),
                ('GET', f'/api/v1/backups/{first}/delete', None, 'method_not_allowed'),
                ('POST', '/api/v1/backups/bkp_' + '0' * 32 + '/delete', {'confirm': True}, 'not_found')):
            with self.subTest(target=target, body=body), self.assertRaises(OPS.OpsError) as caught:
                OPS.handle(self.sources, method, target, body)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(OPS.handle(self.sources, 'GET', '/api/v1/backups')[1]['items']), 2)

    def test_does_not_follow_a_linked_backup_folder(self):
        self.make(1)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'precious').write_text('keep')
        link = self.sources.backup_root / 'aaaaaaaa-20260115T140000Z-linked'
        link.symlink_to(outside, target_is_directory=True)
        private(outside / 'manifest.json', {'revision': 'a' * 40, 'created_utc': '20260115T140000Z'})
        linked_id = self.sources.public_id('backup', link.name)
        with self.assertRaises(OPS.OpsError) as caught:
            OPS.handle(self.sources, 'POST', f'/api/v1/backups/{linked_id}/delete', {'confirm': True})
        self.assertEqual(caught.exception.code, 'not_found')
        self.assertTrue((outside / 'precious').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
