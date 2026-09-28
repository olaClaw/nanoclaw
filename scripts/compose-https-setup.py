#!/usr/bin/env python3
"""Guided HTTPS setup for the dashboard (run as root on the Compose host).

Configures the `proxy` service (Nginx Proxy Manager) through its API so its
admin UI never needs to be opened:

  1. asks for the panel's hostname, the LAN/VPN address to publish 443 on,
     and the certificate mode:
       - `letsencrypt`: DNS-01 with one of the providers the proxy image ships
         (credentials typed hidden, never echoed, logged or passed as
         arguments; they are stored only inside the proxy's own database);
       - `local`: a certificate generated here, for installs without a domain
         (the connection is encrypted; browsers warn until the certificate is
         trusted on the device);
       - `http`: no certificate and no proxy, for a trusted LAN/VPN only. The
         panel is published unencrypted on that address; asked only after an
         explicit confirmation, and the panel warns on every page;
  2. writes NANOCLAW_DASHBOARD_ORIGIN and NANOCLAW_PROXY_BIND in the private
     .env and recreates `dashboard` and `proxy`;
  3. creates the proxy administrator on a fresh proxy (random password shown
     once) or signs in with an existing one;
  4. obtains or installs the certificate, creates or updates the proxy host
     `https://<hostname>` -> `http://dashboard:8080` (Force SSL, HTTP/2, HSTS);
  5. checks that the panel answers over HTTPS.

In `http` mode steps 3-5 are replaced by: stop the proxy, publish the
dashboard on the address (NANOCLAW_DASHBOARD_HTTP_BIND, the plain-HTTP
opt-in NANOCLAW_DASHBOARD_INSECURE_HTTP and an http origin), check that it
answers. Running an https mode later switches the opt-in off again.

Safe to run again: existing certificates and proxy hosts for the hostname are
reused. `--check` shows the plan and changes nothing. Prints fixed status
labels only, never tokens or passwords (except the new proxy password, once,
on the terminal).
"""

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HOSTNAME = re.compile(r'(?=.{1,253}\Z)(?:(?!-)[a-z0-9-]{1,63}(?<!-)\.)+[a-z][a-z0-9-]{0,62}(?<!-)\Z')
EMAIL = re.compile(r'[^@\s]{1,64}@[^@\s]{1,253}\.[a-z]{2,}\Z', re.I)
PROVIDER = re.compile(r'[a-z0-9_-]{1,40}\Z')
CREDENTIAL_LINE = re.compile(r'\s*([a-z0-9_]+)\s*=\s*(.*)\Z')
PROFILES = ('--profile', 'core-preview', '--profile', 'dashboard')
STATE_DIRS = ('/srv/nanoclaw/proxy/data', '/srv/nanoclaw/proxy/letsencrypt', '/srv/nanoclaw/dashboard')


class SetupError(Exception):
    pass


def fail(code):
    raise SetupError(code)


def say(text):
    print(text, flush=True)


# ── Environment file ──

def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    return values


def set_env(path, updates):
    """Replace or append keys, keeping owner, mode and every other line."""
    info = path.stat()
    lines = path.read_text().splitlines(keepends=True)
    seen = set()
    for index, line in enumerate(lines):
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else None
        if key in updates:
            lines[index] = f'{key}={updates[key]}\n'
            seen.add(key)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    lines += [f'{key}={value}\n' for key, value in updates.items() if key not in seen]
    fd, temporary = tempfile.mkstemp(prefix='.env.https-', dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IMODE(info.st_mode))
        os.fchown(fd, info.st_uid, info.st_gid)
        with os.fdopen(fd, 'w') as stream:
            stream.write(''.join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# ── Proxy API ──

class Proxy:
    def __init__(self, base, opener=None):
        self.base = base.rstrip('/')
        self.token = None
        self.opener = opener or urllib.request.urlopen

    def call(self, method, path, body=None, timeout=30):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header('Accept', 'application/json')
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        if self.token:
            request.add_header('Authorization', f'Bearer {self.token}')
        try:
            with self.opener(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw) if raw else None
            except ValueError:
                return error.code, None

    def wait_ready(self, seconds=180):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                status, body = self.call('GET', '/')
                if status == 200 and isinstance(body, dict) and body.get('status') == 'OK':
                    return body
            except (OSError, ValueError):
                pass
            time.sleep(2)
        fail('proxy_api_unavailable')

    def login(self, email, password):
        status, body = self.call('POST', '/tokens', {'identity': email, 'secret': password})
        if status != 200 or not isinstance(body, dict) or not body.get('token'):
            fail('proxy_login_failed')
        self.token = body['token']

    def create_admin(self, email, password):
        body = {'name': 'Administrator', 'nickname': 'Admin', 'email': email, 'roles': ['admin'],
                'is_disabled': False, 'auth': {'type': 'password', 'secret': password}}
        status, _ = self.call('POST', '/users', body)
        if status not in (200, 201):
            fail('proxy_admin_not_created')


# ── Certificates ──

def dns_plugins(project):
    """The DNS providers the pinned proxy image ships, with their credential templates."""
    config = json.loads(compose(project, 'config', '--format', 'json', capture=True))
    image = config.get('services', {}).get('proxy', {}).get('image')
    if not isinstance(image, str) or '@sha256:' not in image:
        fail('proxy_image_unpinned')
    result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'cat', image,
                             '/app/certbot/dns-plugins.json'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=300, check=False)
    if result.returncode:
        fail('dns_plugins_unavailable')
    plugins = json.loads(result.stdout)
    if not isinstance(plugins, dict) or not plugins:
        fail('dns_plugins_unavailable')
    return plugins


def credential_keys(template):
    keys = []
    for line in str(template).splitlines():
        match = CREDENTIAL_LINE.fullmatch(line)
        if match:
            keys.append(match.group(1))
    return keys


def local_certificate(hostname, directory):
    key, cert = directory / 'panel.key', directory / 'panel.crt'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                    '-days', '825', '-subj', f'/CN={hostname}', '-addext', f'subjectAltName=DNS:{hostname}',
                    '-keyout', str(key), '-out', str(cert)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    os.chmod(key, 0o600)
    return cert, key


def upload_certificate(proxy, certificate_id, cert, key):
    boundary = 'nanoclaw' + secrets.token_hex(8)
    parts = []
    for name, path in (('certificate', cert), ('certificate_key', key)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n'.encode() + path.read_bytes() + b'\r\n')
    body = b''.join(parts) + f'--{boundary}--\r\n'.encode()
    request = urllib.request.Request(f'{proxy.base}/nginx/certificates/{certificate_id}/upload', data=body,
                                     method='POST')
    request.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    request.add_header('Authorization', f'Bearer {proxy.token}')
    try:
        with proxy.opener(request, timeout=60) as response:
            if response.status != 200:
                fail('certificate_upload_failed')
    except urllib.error.HTTPError:
        fail('certificate_upload_failed')


def find_certificate(proxy, hostname):
    status, items = proxy.call('GET', '/nginx/certificates')
    if status != 200 or not isinstance(items, list):
        fail('proxy_api_error')
    for item in items:
        # Uploaded (local) certificates carry no domain in the proxy's record: match them by name.
        if isinstance(item, dict) and (hostname in (item.get('domain_names') or []) or
                                       (item.get('provider') == 'other' and item.get('nice_name') == hostname)):
            return item
    return None


def ensure_certificate(proxy, answers, workdir):
    existing = find_certificate(proxy, answers['hostname'])
    if existing:
        say('certificate=reused')
        return existing['id']
    if answers['mode'] == 'local':
        status, body = proxy.call('POST', '/nginx/certificates', {'provider': 'other', 'nice_name': answers['hostname']})
        if status not in (200, 201) or not isinstance(body, dict):
            fail('certificate_not_created')
        cert, key = local_certificate(answers['hostname'], workdir)
        upload_certificate(proxy, body['id'], cert, key)
        say('certificate=local')
        return body['id']
    meta = {'dns_challenge': True, 'dns_provider': answers['provider'],
            'dns_provider_credentials': answers['credentials'],
            'propagation_seconds': answers.get('propagation_seconds', 120), 'key_type': 'ecdsa'}
    say('certificate=requesting (DNS-01; this takes a few minutes)')
    status, body = proxy.call('POST', '/nginx/certificates',
                              {'provider': 'letsencrypt', 'nice_name': answers['hostname'],
                               'domain_names': [answers['hostname']], 'meta': meta}, timeout=900)
    if status not in (200, 201) or not isinstance(body, dict) or not body.get('id'):
        fail('certificate_not_issued')
    say('certificate=issued')
    return body['id']


def ensure_proxy_host(proxy, hostname, certificate_id):
    status, items = proxy.call('GET', '/nginx/proxy-hosts')
    if status != 200 or not isinstance(items, list):
        fail('proxy_api_error')
    body = {'domain_names': [hostname], 'forward_scheme': 'http', 'forward_host': 'dashboard', 'forward_port': 8080,
            'certificate_id': certificate_id, 'ssl_forced': True, 'http2_support': True, 'hsts_enabled': True,
            'hsts_subdomains': False, 'block_exploits': True, 'caching_enabled': False,
            'allow_websocket_upgrade': False, 'access_list_id': 0, 'advanced_config': '', 'locations': [],
            'meta': {}}
    existing = next((item for item in items if isinstance(item, dict) and hostname in (item.get('domain_names') or [])),
                    None)
    if existing:
        status, _ = proxy.call('PUT', f'/nginx/proxy-hosts/{existing["id"]}', body)
        state = 'updated'
    else:
        status, _ = proxy.call('POST', '/nginx/proxy-hosts', body)
        state = 'created'
    if status not in (200, 201):
        fail('proxy_host_not_saved')
    say(f'proxy_host={state}')


# ── Host commands ──

def compose(project, *args, capture=False):
    result = subprocess.run(['docker', 'compose', '--env-file', '.env', *PROFILES, *args], cwd=project,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=600, check=False)
    if result.returncode:
        fail('compose_failed')
    return result.stdout.decode() if capture else ''


def https_check(hostname, address, verify, seconds=120):
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((address, 443), timeout=5) as raw:
                with context.wrap_socket(raw, server_hostname=hostname) as tls:
                    tls.sendall(f'GET /api/v1/health HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n'.encode())
                    reply = tls.recv(64)
            if reply.startswith(b'HTTP/1.1 200'):
                return True
        except (OSError, ssl.SSLError):
            pass
        time.sleep(3)
    return False


# ── Questions ──

def ask(prompt, validate, secret=False, default=None):
    for _ in range(5):
        shown = f'{prompt} [{default}]: ' if default else f'{prompt}: '
        value = (getpass.getpass(shown) if secret else input(shown)).strip() or (default or '')
        if validate(value):
            return value
        say('valore non valido, riprova')
    fail('too_many_invalid_answers')


def gather(args, env, plugins):
    if args.answers:
        path = Path(args.answers)
        info = path.stat()
        if info.st_mode & 0o077:
            fail('answers_file_not_private')
        answers = json.loads(path.read_text())
    else:
        answers = {}
        answers['mode'] = ask('Modalità: "letsencrypt" (serve un dominio), "local" (senza dominio, cifrata) '
                              'o "http" (senza cifratura)',
                              lambda v: v in ('letsencrypt', 'local', 'http'), default='letsencrypt')
        if answers['mode'] == 'http':
            say(HTTP_WARNING)
            answers['confirm_insecure'] = ask('Scrivi HTTP per confermare', lambda v: v == 'HTTP') == 'HTTP'
            answers['bind'] = ask('Indirizzo LAN/VPN su cui pubblicare il pannello', lambda v: valid_bind(v),
                                  default=env.get('NANOCLAW_DASHBOARD_HTTP_BIND') or env.get('NANOCLAW_PROXY_BIND') or None)
            return check_answers(answers, plugins)
        answers['hostname'] = ask('Nome del pannello (es. panel.example.org)', lambda v: bool(HOSTNAME.fullmatch(v.lower()))).lower()
        answers['bind'] = ask('Indirizzo LAN/VPN su cui pubblicare la porta 443',
                              lambda v: valid_bind(v), default=env.get('NANOCLAW_PROXY_BIND') or None)
        answers['email'] = ask('Email per l\'amministratore del proxy (e per Let\'s Encrypt)',
                               lambda v: bool(EMAIL.fullmatch(v)))
        if answers['mode'] == 'letsencrypt':
            say('Provider DNS disponibili: ' + ', '.join(sorted(plugins)))
            answers['provider'] = ask('Provider DNS del dominio', lambda v: v in plugins)
            lines = []
            for key in credential_keys(plugins[answers['provider']].get('credentials', '')):
                value = ask(f'{key} (non viene mostrato)', lambda v: bool(v) and '\n' not in v, secret=True)
                lines.append(f'{key} = {value}')
            if not lines:
                fail('provider_credentials_unknown')
            answers['credentials'] = '\n'.join(lines)
    return check_answers(answers, plugins)


def check_answers(answers, plugins):
    if answers.get('mode') == 'http':
        if answers.get('confirm_insecure') is not True or not valid_bind(str(answers.get('bind', ''))):
            fail('invalid_answers')
        return answers
    answers['hostname'] = str(answers.get('hostname', '')).lower()
    if not HOSTNAME.fullmatch(answers['hostname']) or not valid_bind(str(answers.get('bind', ''))):
        fail('invalid_answers')
    if answers.get('mode') not in ('letsencrypt', 'local') or not EMAIL.fullmatch(str(answers.get('email', ''))):
        fail('invalid_answers')
    if answers['mode'] == 'letsencrypt' and (answers.get('provider') not in plugins or not answers.get('credentials')):
        fail('invalid_answers')
    return answers


HTTP_WARNING = ('ATTENZIONE: in modalità "http" la connessione al pannello NON è cifrata. Password, '
                'chiavi dei backup e dati passano in chiaro sulla rete: chiunque sulla stessa rete può '
                'leggerli. Usala solo su una LAN/VPN di cui ti fidi e passa a HTTPS appena possibile.')


def origin_host(address):
    return f'[{address}]' if ':' in address else address


def http_check(address, port, seconds=60):
    deadline = time.time() + seconds
    url = f'http://{origin_host(address)}:{port}/api/v1/health'
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(3)
    return False


def valid_bind(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not address.is_unspecified and not address.is_multicast


def plain_http(project, env_file, env, answers, check):
    port = env.get('NANOCLAW_DASHBOARD_LOOPBACK_PORT') or '18080'
    origin = f'http://{origin_host(answers["bind"])}:{port}'
    say(f'plan=mode:http origin_change:{"no" if env.get("NANOCLAW_DASHBOARD_ORIGIN") == origin else "yes"} '
        'encryption:none')
    if check:
        say('check=ok (nothing changed)')
        return 0
    set_env(env_file, {'NANOCLAW_DASHBOARD_ORIGIN': origin, 'NANOCLAW_DASHBOARD_INSECURE_HTTP': 'true',
                       'NANOCLAW_DASHBOARD_HTTP_BIND': answers['bind']})
    say('env=updated')
    # No proxy in this mode: it would keep answering on 443 for the old origin.
    compose(project, 'stop', 'proxy')
    compose(project, 'up', '-d', '--wait', '--no-deps', '--force-recreate', 'dashboard')
    say('services=recreated')
    ok = http_check(answers['bind'], port)
    say(f'http={"ok" if ok else "not_answering"} encryption=none')
    if ok:
        say(f'Pannello: {origin} (non cifrato)')
    return 0 if ok else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project-root', required=True, help='the Compose checkout (with its private .env)')
    parser.add_argument('--answers', help='private JSON file with the answers, for unattended runs')
    parser.add_argument('--check', action='store_true', help='show the plan, change nothing')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        fail('root_required')
    project = Path(args.project_root).resolve()
    env_file = project / '.env'
    if not env_file.is_file():
        fail('env_missing')
    env = read_env(env_file)
    for directory in STATE_DIRS:
        if not Path(directory).is_dir():
            fail('state_directories_missing')
    answers = gather(args, env, dns_plugins(project))
    if answers['mode'] == 'http':
        return plain_http(project, env_file, env, answers, args.check)
    origin = f'https://{answers["hostname"]}'
    say(f'plan=hostname:{answers["hostname"]} mode:{answers["mode"]} origin_change:'
        f'{"no" if env.get("NANOCLAW_DASHBOARD_ORIGIN") == origin else "yes"}'
        f' bind_change:{"no" if env.get("NANOCLAW_PROXY_BIND") == answers["bind"] else "yes"}')
    if args.check:
        say('check=ok (nothing changed)')
        return 0
    # An https mode always switches the plain-HTTP opt-in off again.
    set_env(env_file, {'NANOCLAW_DASHBOARD_ORIGIN': origin, 'NANOCLAW_PROXY_BIND': answers['bind'],
                       'NANOCLAW_DASHBOARD_INSECURE_HTTP': '', 'NANOCLAW_DASHBOARD_HTTP_BIND': ''})
    say('env=updated')
    compose(project, 'up', '-d', '--wait', '--no-deps', '--force-recreate', 'dashboard', 'proxy')
    say('services=recreated')

    proxy = Proxy(f'http://127.0.0.1:{env.get("NANOCLAW_PROXY_ADMIN_PORT") or "18081"}/api')
    status = proxy.wait_ready()
    new_password = None
    if not status.get('setup'):
        new_password = secrets.token_urlsafe(24)
        proxy.create_admin(answers['email'], new_password)
        proxy.login(answers['email'], new_password)
        say('proxy_admin=created')
    else:
        password = answers.get('proxy_password') or getpass.getpass('Password dell\'amministratore del proxy: ')
        proxy.login(answers['email'], password)
        say('proxy_admin=existing')
    with tempfile.TemporaryDirectory() as work:
        certificate_id = ensure_certificate(proxy, answers, Path(work))
    ensure_proxy_host(proxy, answers['hostname'], certificate_id)
    ok = https_check(answers['hostname'], answers['bind'], verify=answers['mode'] == 'letsencrypt')
    say(f'https={"ok" if ok else "not_answering"}')
    if new_password:
        say('')
        say('Password dell\'amministratore del proxy (mostrata solo ora, salvala nel password manager):')
        say(f'  utente: {answers["email"]}')
        say(f'  password: {new_password}')
    return 0 if ok else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SetupError as error:
        say(f'https_setup=failed failure_category={error}')
        sys.exit(2)
    except KeyboardInterrupt:
        say('https_setup=cancelled')
        sys.exit(130)
