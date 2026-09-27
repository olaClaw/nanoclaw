#!/usr/bin/env python3
"""Root-side operations service for the dashboard (read-only first slice).

Runs on the Compose host as root, outside Docker, next to the release and
backup tools it reports on. It answers a few contract endpoints
(src/dashboard/contract/api.ts) over a Unix socket that only the dashboard's
group can open:

  GET /api/v1/releases      installed release, verified candidate, last update job
  GET /api/v1/backups       encrypted backups (no paths, no keys)
  GET /api/v1/jobs/{job}    one release-update job

Everything else is `not_found` or `not_implemented`. Responses are built from
fixed codes, revisions, versions and UTC times only; backup IDs are HMACs of
the backup folder name under the dashboard's public-ID key, so the dashboard
and this service agree on them. It never starts an operation in this slice.
"""

import argparse
import grp
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import socketserver
import stat
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

PAGE_SIZE = 50
REVISION = re.compile(r'[0-9a-f]{40}\Z')
VERSION = re.compile(r'\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z')
JOB_ID = re.compile(r'job_[0-9a-f]{16}\Z')
BACKUP_ID = re.compile(r'bkp_[0-9a-f]{32}\Z')
CURSOR = re.compile(r'o\d{1,6}\Z')
CODE = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
STAMP = re.compile(r'\d{8}T\d{6}Z\Z')
JOB_FILE = 'compose-release-update.job.json'
JOB_HISTORY = 'compose-release-update.jobs.jsonl'
PHASES = {'preflight', 'control_backup', 'stop_host', 'switch_release', 'start_services',
          'wait_channels', 'refresh_images', 'refresh_dashboard', 'rollback'}
OUTCOMES = {'running', 'preflight_ok', 'succeeded', 'failed', 'rolled_back', 'rollback_failed', 'interrupted'}
ROLLBACKS = {'not_needed', 'healthy', 'failed_manual_recovery_needed'}
MAX_BODY = 64 * 1024


class OpsError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def iso(value):
    """ISO-8601 UTC with Z and no fraction, or None."""
    if not isinstance(value, str):
        return None
    try:
        if STAMP.fullmatch(value):
            moment = datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        else:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def private_json(path, limit=64 * 1024):
    """A root-owned, private JSON file, or None when missing or unsafe."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > limit:
        return None
    try:
        return json.loads(path.read_bytes())
    except ValueError:
        return None


class Sources:
    def __init__(self, state_root, backup_root, control_root, candidates, id_key):
        self.state_root = Path(state_root)
        self.backup_root = Path(backup_root)
        self.control_root = Path(control_root)
        self.candidates = Path(candidates) if candidates else None
        if len(id_key) < 32:
            raise ValueError('id key too short')
        self.id_key = id_key

    def public_id(self, kind, internal):
        digest = hmac.new(self.id_key, f'{kind}\0{internal}'.encode(), hashlib.sha256).hexdigest()[:32]
        return {'backup': 'bkp_'}[kind] + digest

    def release(self, path):
        record = private_json(path)
        if not isinstance(record, dict):
            return None
        version, revision = record.get('version'), record.get('revision')
        if isinstance(version, str) and VERSION.fullmatch(version) and isinstance(revision, str) \
                and REVISION.fullmatch(revision):
            return {'version': version, 'revision': revision}
        return None

    def job(self, record):
        """Project a release-update job record onto the contract's job shape."""
        if not isinstance(record, dict) or not JOB_ID.fullmatch('job_' + str(record.get('job_id', ''))):
            return None
        phase = record.get('phase') if record.get('phase') in PHASES else 'preflight'
        outcome = record.get('outcome') if record.get('outcome') in OUTCOMES else 'failed'
        category = record.get('failure_category')
        rollback = record.get('rollback')
        started = iso(record.get('started_utc'))
        if not started:
            return None
        release = {
            'from_revision': record.get('from_revision') if REVISION.fullmatch(str(record.get('from_revision', ''))) else None,
            'to_revision': record.get('to_revision') if REVISION.fullmatch(str(record.get('to_revision', ''))) else None,
            'to_version': record.get('to_version') if VERSION.fullmatch(str(record.get('to_version', ''))) else None,
        }
        phases = []
        for step in record.get('phases', [])[:32] if isinstance(record.get('phases'), list) else []:
            at = iso(step.get('at')) if isinstance(step, dict) else None
            if at and step.get('phase') in PHASES:
                phases.append({'phase': step['phase'], 'at': at})
        return {
            'id': 'job_' + record['job_id'],
            'kind': 'update',
            'phase': phase,
            'outcome': outcome,
            'failure_category': category if isinstance(category, str) and CODE.fullmatch(category) else None,
            'rollback': rollback if rollback in ROLLBACKS else None,
            'release': release,
            'phases': phases,
            'started_at': started,
            'updated_at': iso(record.get('updated_utc')) or started,
            'finished_at': iso(record.get('finished_utc')),
        }

    def update_jobs(self):
        """Current record first, then history, newest first; each job once."""
        seen, jobs = set(), []
        current = private_json(self.control_root / JOB_FILE)
        records = [current] if current else []
        history = self.control_root / JOB_HISTORY
        try:
            info = history.lstat()
            if stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022 and info.st_size <= 4 * 1024 * 1024:
                for line in reversed(history.read_text().splitlines()):
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        for record in records:
            projected = self.job(record)
            if projected and projected['id'] not in seen:
                seen.add(projected['id'])
                jobs.append(projected)
        return jobs

    def candidate(self, installed):
        """The newest well-formed manifest in the candidates directory newer than the installed one."""
        if not self.candidates or not self.candidates.is_dir():
            return None
        best = None
        for path in sorted(self.candidates.glob('*.json')):
            release = self.release(path)
            if release and (not installed or release['revision'] != installed['revision']):
                best = release
        return {**best, 'verified': True} if best else None

    def backups(self):
        items = []
        if not self.backup_root.is_dir():
            return items
        for folder in self.backup_root.iterdir():
            if folder.is_symlink() or not folder.is_dir():
                continue
            manifest = private_json(folder / 'manifest.json')
            if not isinstance(manifest, dict):
                continue
            created = iso(manifest.get('created_utc'))
            revision = manifest.get('revision')
            if not created or not isinstance(revision, str) or not REVISION.fullmatch(revision):
                continue
            size = sum(item.stat().st_size for item in folder.iterdir()
                       if item.is_file() and not item.is_symlink())
            items.append({
                'id': self.public_id('backup', folder.name),
                'created_at': created,
                'release_revision': revision,
                'size_bytes': size,
                # The backup tool writes the manifest only after verifying the archive.
                'verification': 'verified' if isinstance(manifest.get('hmac_sha256'), str) else 'unverified',
                'exportable': False,
            })
        items.sort(key=lambda item: item['created_at'], reverse=True)
        return items


def page(items, cursor):
    offset = int(cursor[1:]) if cursor else 0
    chunk = items[offset:offset + PAGE_SIZE]
    following = f'o{offset + PAGE_SIZE}' if offset + PAGE_SIZE < len(items) else None
    return {'items': chunk, 'next_cursor': following}


def handle(sources, method, target):
    """Return (status, body) for one request; raises OpsError for refusals."""
    if not isinstance(target, str) or len(target) > 512 or not target.startswith('/'):
        raise OpsError(400, 'invalid_request')
    parts = urlsplit(target)
    query = parse_qsl(parts.query, keep_blank_values=True)
    path = parts.path
    routes = {'/api/v1/releases': 'releases', '/api/v1/backups': 'backups'}
    name = routes.get(path)
    job_match = re.fullmatch(r'/api/v1/jobs/([^/]+)', path)
    if name is None and not job_match:
        if path.startswith('/api/v1/backups/') or path in ('/api/v1/updates', '/api/v1/imports/preflight'):
            raise OpsError(501, 'not_implemented')
        raise OpsError(404, 'not_found')
    if method != 'GET':
        raise OpsError(405, 'method_not_allowed')
    cursor = None
    if query:
        if name != 'backups' or len(query) != 1 or query[0][0] != 'cursor' or not CURSOR.fullmatch(query[0][1]):
            raise OpsError(400, 'invalid_query')
        cursor = query[0][1]
    if job_match:
        job_id = job_match.group(1)
        if not JOB_ID.fullmatch(job_id):
            raise OpsError(404, 'not_found')
        for job in sources.update_jobs():
            if hmac.compare_digest(job['id'], job_id):
                return 200, job
        raise OpsError(404, 'not_found')
    if name == 'releases':
        installed = sources.release(sources.state_root / 'release.json')
        if not installed:
            raise OpsError(503, 'release_unknown')
        jobs = sources.update_jobs()
        return 200, {'installed': installed, 'candidate': sources.candidate(installed),
                     'last_update': jobs[0] if jobs else None}
    return 200, page(sources.backups(), cursor)


HEADERS = {'Cache-Control': 'no-store', 'Content-Type': 'application/json; charset=utf-8',
           'X-Content-Type-Options': 'nosniff'}


def make_handler(sources):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'nanoclaw-ops'
        sys_version = ''

        def log_message(self, *_args):
            pass  # never log paths, queries or bodies

        def respond(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            for key, value in HEADERS.items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def dispatch(self):
            request_id = 'req_' + secrets.token_hex(8)
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if length > MAX_BODY:
                    raise OpsError(413, 'payload_too_large')
                if length:
                    self.rfile.read(length)
                    raise OpsError(501, 'not_implemented')
                status, body = handle(sources, self.command, self.path)
            except OpsError as error:
                status, body = error.status, {'error': {'code': error.code, 'request_id': request_id}}
            except Exception:  # fail closed without detail
                status, body = 500, {'error': {'code': 'internal_error', 'request_id': request_id}}
            self.respond(status, body)

        do_GET = do_POST = do_DELETE = dispatch

    return Handler


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        request.settimeout(15)
        return request, ('ops', 0)


def serve(args):
    if os.geteuid() != 0:
        raise SystemExit('compose-ops: must run as root')
    key = Path(args.id_key).read_bytes()
    sources = Sources(args.state_root, args.backup_root, args.control_backup_root, args.candidates, key)
    sock = Path(args.socket)
    directory = sock.parent
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o007:
        raise SystemExit('compose-ops: socket directory must be closed to others')
    if sock.exists():
        if not stat.S_ISSOCK(sock.lstat().st_mode):
            raise SystemExit('compose-ops: socket path is not a socket')
        sock.unlink()
    gid = grp.getgrnam(args.socket_group).gr_gid if not args.socket_group.isdigit() else int(args.socket_group)
    old = os.umask(0o117)
    try:
        server = UnixServer(str(sock), make_handler(sources))
    finally:
        os.umask(old)
    os.chown(sock, 0, gid)
    os.chmod(sock, 0o660)
    print('compose-ops listening', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        sock.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('serve')
    s.add_argument('--socket', required=True)
    s.add_argument('--socket-group', required=True, help='group allowed to connect (the dashboard GID)')
    s.add_argument('--state-root', default='/srv/nanoclaw')
    s.add_argument('--backup-root', required=True)
    s.add_argument('--control-backup-root', required=True)
    s.add_argument('--candidates', default=None, help='directory of verified candidate release manifests')
    s.add_argument('--id-key', default='/srv/nanoclaw/data/dashboard/id-key')
    args = parser.parse_args(argv)
    serve(args)


if __name__ == '__main__':
    main()
