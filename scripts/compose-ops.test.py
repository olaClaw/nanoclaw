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
                 ('POST', '/api/v1/updates', 'not_implemented'), ('POST', '/api/v1/backups/bkp_x/verify', 'not_implemented'),
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
                status, _, body = call('POST', '/api/v1/updates', b'{"confirm":true}')
                self.assertEqual((status, body['error']['code']), (501, 'not_implemented'))
                self.assertRegex(body['error']['request_id'], r'^req_[0-9a-f]{16}$')
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
