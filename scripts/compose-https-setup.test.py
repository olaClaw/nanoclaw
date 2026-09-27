import contextlib
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name('compose-https-setup.py')
SPEC = importlib.util.spec_from_file_location('compose_https_setup', MODULE_PATH)
SETUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETUP)

TOKEN = 'fixture-dns-token-7f3a'
PLUGINS = {'infomaniak': {'name': 'Infomaniak', 'credentials': 'dns_infomaniak_token = XXXXXXXX'},
           'twokeys': {'name': 'Two keys', 'credentials': 'dns_a_key = X\ndns_a_secret = Y'}}


class FakeProxy:
    """Enough of the proxy's API for the setup flow; records what it receives."""

    def __init__(self):
        self.users, self.certificates, self.hosts, self.uploads, self.requests = [], [], [], [], []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def body(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                if self.headers.get('Content-Type', '').startswith('multipart/'):
                    return raw
                return json.loads(raw) if raw else None

            def authorized(self):
                return self.headers.get('Authorization') == 'Bearer fixture-session'

            def handle_any(self):
                body = self.body() if self.command in ('POST', 'PUT') else None
                state.requests.append((self.command, self.path, body))
                path = self.path
                if path == '/api/' and self.command == 'GET':
                    return self.reply(200, {'status': 'OK', 'setup': bool(state.users)})
                if path == '/api/users' and self.command == 'POST':
                    if state.users and not self.authorized():
                        return self.reply(403, {})
                    state.users.append(body)
                    return self.reply(201, {'id': len(state.users)})
                if path == '/api/tokens' and self.command == 'POST':
                    ok = any(u['email'] == body['identity'] and u['auth']['secret'] == body['secret'] for u in state.users)
                    return self.reply(200, {'token': 'fixture-session'}) if ok else self.reply(400, {})
                if not self.authorized():
                    return self.reply(401, {})
                if path == '/api/nginx/certificates':
                    if self.command == 'GET':
                        return self.reply(200, state.certificates)
                    item = {'id': len(state.certificates) + 1, **body}
                    item.setdefault('domain_names', [])
                    state.certificates.append(item)
                    return self.reply(201, item)
                if path.startswith('/api/nginx/certificates/') and path.endswith('/upload'):
                    state.uploads.append(body)
                    return self.reply(200, {})
                if path == '/api/nginx/proxy-hosts':
                    if self.command == 'GET':
                        return self.reply(200, state.hosts)
                    state.hosts.append({'id': len(state.hosts) + 1, **body})
                    return self.reply(201, state.hosts[-1])
                if path.startswith('/api/nginx/proxy-hosts/') and self.command == 'PUT':
                    index = int(path.rsplit('/', 1)[1]) - 1
                    state.hosts[index] = {'id': index + 1, **body}
                    return self.reply(200, state.hosts[index])
                return self.reply(404, {})

            do_GET = do_POST = do_PUT = handle_any

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HttpsSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.proxy = FakeProxy()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.env = self.project / '.env'
        self.env.write_text(f'NANOCLAW_INSTALL_ID=fixture\nNANOCLAW_PROXY_ADMIN_PORT={self.proxy.port}\n# comment=kept\n')
        os.chmod(self.env, 0o600)
        self.compose_calls = []

    def tearDown(self):
        self.proxy.close()
        self.temp.cleanup()

    def run_setup(self, answers, *extra):
        path = self.root / 'answers.json'
        path.write_text(json.dumps(answers))
        os.chmod(path, 0o600)
        out = io.StringIO()
        with (patch.object(SETUP.os, 'geteuid', return_value=0),
              patch.object(SETUP, 'STATE_DIRS', (str(self.root),)),
              patch.object(SETUP, 'dns_plugins', return_value=PLUGINS),
              patch.object(SETUP, 'compose', side_effect=lambda _p, *a, **_k: self.compose_calls.append(a) or ''),
              patch.object(SETUP, 'https_check', return_value=True),
              patch.object(SETUP, 'local_certificate', side_effect=self.fake_local_certificate),
              contextlib.redirect_stdout(out)):
            try:
                code = SETUP.main(['--project-root', str(self.project), '--answers', str(path), *extra])
            except SETUP.SetupError as error:
                code = str(error)
        return code, out.getvalue()

    def fake_local_certificate(self, hostname, directory):
        (directory / 'panel.crt').write_text('CERT ' + hostname)
        (directory / 'panel.key').write_text('KEY')
        return directory / 'panel.crt', directory / 'panel.key'

    def answers(self, **changes):
        return {'hostname': 'Panel.Example.Invalid', 'bind': '192.0.2.10', 'mode': 'letsencrypt',
                'email': 'admin@example.invalid', 'provider': 'infomaniak',
                'credentials': f'dns_infomaniak_token = {TOKEN}', **changes}

    def test_fresh_proxy_is_configured_end_to_end_without_echoing_the_token(self):
        code, output = self.run_setup(self.answers())
        self.assertEqual(code, 0)
        env = self.env.read_text()
        self.assertIn('NANOCLAW_DASHBOARD_ORIGIN=https://panel.example.invalid\n', env)
        self.assertIn('NANOCLAW_PROXY_BIND=192.0.2.10\n', env)
        self.assertIn('NANOCLAW_INSTALL_ID=fixture\n', env)
        self.assertIn('# comment=kept\n', env)
        self.assertEqual(os.stat(self.env).st_mode & 0o777, 0o600)
        self.assertEqual(self.proxy.users[0]['email'], 'admin@example.invalid')
        certificate = self.proxy.certificates[0]
        self.assertEqual((certificate['provider'], certificate['domain_names']), ('letsencrypt', ['panel.example.invalid']))
        self.assertEqual(certificate['meta']['dns_provider_credentials'], f'dns_infomaniak_token = {TOKEN}')
        host = self.proxy.hosts[0]
        self.assertEqual((host['forward_host'], host['forward_port'], host['certificate_id']), ('dashboard', 8080, 1))
        self.assertTrue(host['ssl_forced'] and host['http2_support'] and host['hsts_enabled'])
        self.assertNotIn(TOKEN, output)
        self.assertEqual(output.count(self.proxy.users[0]['auth']['secret']), 1)
        self.assertIn('https=ok', output)
        self.assertIn(('up', '-d', '--wait', '--no-deps', '--force-recreate', 'dashboard', 'proxy'), self.compose_calls)

    def test_rerun_reuses_the_certificate_and_updates_the_host(self):
        self.run_setup(self.answers())
        password = self.proxy.users[0]['auth']['secret']
        code, output = self.run_setup(self.answers(proxy_password=password))
        self.assertEqual(code, 0)
        self.assertEqual((len(self.proxy.users), len(self.proxy.certificates), len(self.proxy.hosts)), (1, 1, 1))
        self.assertIn('certificate=reused', output)
        self.assertIn('proxy_host=updated', output)
        self.assertIn('proxy_admin=existing', output)
        self.assertNotIn('password:', output)

    def test_local_certificate_for_installs_without_a_domain(self):
        code, output = self.run_setup(self.answers(mode='local', provider=None, credentials=None))
        self.assertEqual(code, 0)
        self.assertEqual(self.proxy.certificates[0]['provider'], 'other')
        self.assertIn(b'CERT panel.example.invalid', self.proxy.uploads[0])
        self.assertIn('certificate=local', output)
        password = self.proxy.users[0]['auth']['secret']
        self.run_setup(self.answers(mode='local', provider=None, credentials=None, proxy_password=password))
        self.assertEqual(len(self.proxy.certificates), 1)

    def test_check_changes_nothing(self):
        before = self.env.read_text()
        code, output = self.run_setup(self.answers(), '--check')
        self.assertEqual(code, 0)
        self.assertIn('check=ok', output)
        self.assertEqual(self.env.read_text(), before)
        self.assertEqual((self.proxy.requests, self.compose_calls), ([], []))

    def test_refuses_bad_answers_and_open_answer_files(self):
        for changes in ({'bind': '0.0.0.0'}, {'hostname': 'not a host'}, {'mode': 'plain-http'},
                        {'provider': 'unknown'}, {'credentials': ''}, {'email': 'nobody'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.run_setup(self.answers(**changes))[0], 'invalid_answers')
        path = self.root / 'answers.json'
        with patch.object(SETUP, 'dns_plugins', return_value=PLUGINS):
            path.write_text(json.dumps(self.answers()))
            os.chmod(path, 0o644)
            args = SETUP.argparse.Namespace(answers=str(path))
            with self.assertRaises(SETUP.SetupError) as caught:
                SETUP.gather(args, {}, PLUGINS)
        self.assertEqual(str(caught.exception), 'answers_file_not_private')

    def test_credential_templates_and_env_edits(self):
        self.assertEqual(SETUP.credential_keys(PLUGINS['twokeys']['credentials']), ['dns_a_key', 'dns_a_secret'])
        SETUP.set_env(self.env, {'NANOCLAW_PROXY_BIND': '192.0.2.11'})
        SETUP.set_env(self.env, {'NANOCLAW_PROXY_BIND': '192.0.2.12'})
        self.assertEqual(self.env.read_text().count('NANOCLAW_PROXY_BIND='), 1)
        self.assertIn('NANOCLAW_PROXY_BIND=192.0.2.12', self.env.read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
