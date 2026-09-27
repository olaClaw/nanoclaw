#!/usr/bin/env python3
"""Root-side operations service for the dashboard.

Runs on the Compose host as root, outside Docker, next to the release and
backup tools it reports on. It answers a few contract endpoints
(src/dashboard/contract/api.ts) over a Unix socket that only the dashboard's
group can open:

  GET  /api/v1/releases                    installed release, verified candidate, last update job
  GET  /api/v1/backups                     encrypted backups (no paths, no keys)
  GET  /api/v1/jobs/{job}                  a release-update or backup job
  POST /api/v1/backups                     start an encrypted backup (job)
  POST /api/v1/backups/{backup}/key        the backup's key, while it is still on the server
  POST /api/v1/backups/{backup}/key/saved  the operator saved it: shred it from the server

Everything else is `not_found` or `not_implemented`. Responses are built from
fixed codes, revisions, versions and UTC times only; backup IDs are HMACs of
the backup folder name under the dashboard's public-ID key, so the dashboard
and this service agree on them. The dashboard has already checked the
session, reauth and CSRF; this service still validates every request body.

A backup runs `compose-recovery.py backup --apply` (installed next to this
script) under the release-update lock, so a backup and an update never run
at the same time, whether started here or from the terminal.
"""

import argparse
import fcntl
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
import subprocess
import sys
import threading
import urllib.request
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
LOCK_FILE = '.compose-release-update.lock'
OPS_JOB = re.compile(r'[0-9a-f]{16}\Z')
KEY = re.compile(r'[0-9a-f]{64}\Z')
BACKUP_PHASES = {'backup', 'done'}
UPDATE_PHASES = {'pull', 'backup', 'update', 'done'}
UPDATE_MODES = {'production': '--confirm-production', 'synthetic': '--confirm-synthetic'}


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


def now_utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def write_private(path, value):
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def shred(path):
    """Overwrite then remove a small secret file."""
    size = path.stat().st_size
    with open(path, 'r+b', buffering=0) as stream:
        stream.write(secrets.token_bytes(max(size, 1)))
        os.fsync(stream.fileno())
    path.unlink()


class Sources:
    def __init__(self, state_root, backup_root, control_root, candidates, id_key,
                 key_root=None, project_root=None, jobs_dir=None, recovery_script=None,
                 update_script=None, update_mode=None):
        self.state_root = Path(state_root).resolve()
        self.backup_root = Path(backup_root).resolve()
        self.control_root = Path(control_root).resolve()
        self.candidates = Path(candidates) if candidates else None
        if len(id_key) < 32:
            raise ValueError('id key too short')
        self.id_key = id_key
        # The unit points at links under /var/lib/nanoclaw-ops; the backup tool
        # refuses symlinked paths, so hand it the real directories.
        self.key_root = Path(key_root).resolve() if key_root else None
        self.project_root = Path(project_root).resolve() if project_root else None
        self.jobs_dir = Path(jobs_dir) if jobs_dir else None
        self.recovery_script = Path(recovery_script) if recovery_script else None
        self.update_script = Path(update_script) if update_script else None
        self.update_mode = update_mode if update_mode in UPDATE_MODES else None
        self.running = threading.Lock()

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
            'backup': None,
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
        """The most recently stored manifest in the candidates directory, when it differs from the installed one."""
        if not self.candidates or not self.candidates.is_dir():
            return None
        found = []
        for path in self.candidates.glob('*.json'):
            release = self.release(path)
            if release and (not installed or release['revision'] != installed['revision']):
                found.append((path.stat().st_mtime, release))
        if not found:
            return None
        return {**max(found, key=lambda item: item[0])[1], 'verified': True}

    def backup_folders(self):
        """(public id, folder) for every backup folder with a well-formed manifest."""
        found = []
        if not self.backup_root.is_dir():
            return found
        for folder in self.backup_root.iterdir():
            if folder.is_symlink() or not folder.is_dir():
                continue
            manifest = private_json(folder / 'manifest.json')
            if isinstance(manifest, dict):
                found.append((self.public_id('backup', folder.name), folder))
        return found

    def key_path(self, folder):
        return self.key_root / f'{folder.name}.key' if self.key_root else None

    def key_on_host(self, folder):
        path = self.key_path(folder)
        return bool(path) and path.is_file() and not path.is_symlink()

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
                'key_on_host': self.key_on_host(folder),
            })
        items.sort(key=lambda item: item['created_at'], reverse=True)
        return items


    # ── Operations ──

    def resolve_backup(self, public):
        if not BACKUP_ID.fullmatch(public):
            raise OpsError(404, 'not_found')
        for candidate, folder in self.backup_folders():
            if hmac.compare_digest(candidate, public):
                return folder
        raise OpsError(404, 'not_found')

    def reveal_key(self, public):
        folder = self.resolve_backup(public)
        path = self.key_path(folder)
        if not path or not self.key_on_host(folder):
            raise OpsError(409, 'key_not_on_host')
        info = path.lstat()
        value = path.read_text().strip()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not KEY.fullmatch(value):
            raise OpsError(500, 'key_unreadable')
        return {'backup': public, 'key': value}

    def forget_key(self, public):
        folder = self.resolve_backup(public)
        path = self.key_path(folder)
        if path and self.key_on_host(folder):
            shred(path)
        return {'backup': public, 'key_on_host': False}

    def ops_job_path(self, job_id):
        return self.jobs_dir / f'{job_id}.json'

    def ops_jobs(self):
        if not self.jobs_dir or not self.jobs_dir.is_dir():
            return []
        jobs = []
        for path in self.jobs_dir.glob('*.json'):
            record = private_json(path)
            if isinstance(record, dict) and OPS_JOB.fullmatch(str(record.get('job_id', ''))):
                jobs.append(record)
        return jobs

    def project_ops_job(self, record):
        started = iso(record.get('started_utc'))
        backup = record.get('backup')
        category = record.get('failure_category')
        kind = 'update' if record.get('kind') == 'update' else 'backup_create'
        phases = UPDATE_PHASES if kind == 'update' else BACKUP_PHASES
        release = None
        if kind == 'update':
            target = record.get('to_revision')
            origin = record.get('from_revision')
            version = record.get('to_version')
            release = {'from_revision': origin if REVISION.fullmatch(str(origin or '')) else None,
                       'to_revision': target if REVISION.fullmatch(str(target or '')) else None,
                       'to_version': version if VERSION.fullmatch(str(version or '')) else None}
        rollback = record.get('rollback')
        return {
            'id': 'job_' + record['job_id'],
            'kind': kind,
            'backup': backup if isinstance(backup, str) and BACKUP_ID.fullmatch(backup) else None,
            'phase': record.get('phase') if record.get('phase') in phases else sorted(phases)[0],
            'outcome': record.get('outcome') if record.get('outcome') in OUTCOMES else 'failed',
            'failure_category': category if isinstance(category, str) and CODE.fullmatch(category) else None,
            'rollback': rollback if rollback in ROLLBACKS else None,
            'release': release,
            'phases': [{'phase': p['phase'], 'at': iso(p.get('at'))} for p in record.get('phases', [])
                       if isinstance(p, dict) and p.get('phase') in phases and iso(p.get('at'))][:32],
            'started_at': started,
            'updated_at': iso(record.get('updated_utc')) or started,
            'finished_at': iso(record.get('finished_utc')),
        }

    def find_job(self, public):
        for job in self.update_jobs():
            if hmac.compare_digest(job['id'], public):
                return job
        for record in self.ops_jobs():
            projected = self.project_ops_job(record)
            if projected['started_at'] and hmac.compare_digest(projected['id'], public):
                if projected['outcome'] == 'running' and not self.running.locked():
                    projected['outcome'] = 'interrupted'
                return projected
        return None

    def start_backup(self):
        if not (self.key_root and self.project_root and self.jobs_dir and self.recovery_script):
            raise OpsError(501, 'not_implemented')
        if not self.running.acquire(blocking=False):
            raise OpsError(409, 'operation_in_progress')
        lock = None
        try:
            lock = os.open(self.control_root / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OpsError(409, 'operation_in_progress')
            job_id = secrets.token_hex(8)
            at = now_utc()
            record = {'job_id': job_id, 'kind': 'backup_create', 'phase': 'backup', 'outcome': 'running',
                      'failure_category': None, 'backup': None, 'started_utc': at, 'updated_utc': at,
                      'finished_utc': None, 'phases': [{'phase': 'backup', 'at': at}]}
            write_private(self.ops_job_path(job_id), record)
        except BaseException:
            if lock is not None:
                os.close(lock)
            self.running.release()
            raise
        threading.Thread(target=self.run_backup, args=(record, lock), daemon=True).start()
        return {'job': {'id': 'job_' + job_id, 'kind': 'backup_create'}}

    def run_backup(self, record, lock):
        before = {folder.name for _, folder in self.backup_folders()}
        category, outcome = None, 'failed'
        try:
            result = subprocess.run(
                [sys.executable, str(self.recovery_script), 'backup', '--project-root', str(self.project_root),
                 '--state-root', str(self.state_root), '--backup-root', str(self.backup_root),
                 '--key-root', str(self.key_root), '--apply'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4 * 3600,
                check=False)
            lines = result.stdout.decode('utf-8', 'replace').splitlines()
            for line in lines:
                if line.startswith('failure_category='):
                    category = line.split('=', 1)[1]
            if result.returncode == 0 and 'backup=verified' in lines:
                outcome = 'succeeded'
            elif not category:
                category = 'backup_failed'
            new = [folder for _, folder in self.backup_folders() if folder.name not in before]
            if outcome == 'succeeded' and len(new) == 1:
                record['backup'] = self.public_id('backup', new[0].name)
            elif outcome == 'succeeded':
                outcome, category = 'failed', 'backup_not_found'
        except Exception:
            category = 'unexpected_error'
        finally:
            at = now_utc()
            record.update(outcome=outcome, failure_category=category if CODE.fullmatch(str(category or 'x')) else
                          'unexpected_error', phase='done', updated_utc=at, finished_utc=at)
            if outcome == 'succeeded':
                record['failure_category'] = None
            record['phases'].append({'phase': 'done', 'at': at})
            try:
                write_private(self.ops_job_path(record['job_id']), record)
            finally:
                os.close(lock)
                self.running.release()


def run_tool(argv, timeout):
    result = subprocess.run([sys.executable, *argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    return result.returncode, result.stdout.decode('utf-8', 'replace').splitlines()


def last_value(lines, key):
    values = [line.split('=', 1)[1] for line in lines if line.startswith(key + '=')]
    return values[-1] if values else None


class UpdateRunner:
    """Pull the candidate's images, back up, then run the release-update tool."""

    def __init__(self, sources):
        self.sources = sources

    def start(self, revision):
        src = self.sources
        if not (src.key_root and src.project_root and src.jobs_dir and src.recovery_script
                and src.update_script and src.update_mode and src.candidates):
            raise OpsError(501, 'not_implemented')
        manifest_path = src.candidates / f'{revision}.json'
        manifest = private_json(manifest_path)
        installed = src.release(src.state_root / 'release.json')
        if not isinstance(manifest, dict) or manifest.get('revision') != revision or not installed:
            raise OpsError(409, 'candidate_unknown')
        if not src.running.acquire(blocking=False):
            raise OpsError(409, 'operation_in_progress')
        try:
            lock = os.open(src.control_root / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(lock)
                raise OpsError(409, 'operation_in_progress')
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
            job_id = secrets.token_hex(8)
            at = now_utc()
            record = {'job_id': job_id, 'kind': 'update', 'phase': 'pull', 'outcome': 'running',
                      'failure_category': None, 'rollback': None, 'backup': None,
                      'from_revision': installed['revision'], 'to_revision': revision,
                      'to_version': manifest.get('version'), 'started_utc': at, 'updated_utc': at,
                      'finished_utc': None, 'phases': [{'phase': 'pull', 'at': at}]}
            write_private(src.ops_job_path(job_id), record)
        except BaseException:
            src.running.release()
            raise
        threading.Thread(target=self.run, args=(record, manifest_path, manifest), daemon=True).start()
        return {'job': {'id': 'job_' + job_id, 'kind': 'update'}}

    def phase(self, record, name):
        at = now_utc()
        record.update(phase=name, updated_utc=at)
        record['phases'].append({'phase': name, 'at': at})
        write_private(self.sources.ops_job_path(record['job_id']), record)

    def run(self, record, manifest_path, manifest):
        src = self.sources
        outcome, category, rollback = 'failed', None, None
        try:
            for key in ('host', 'agent', 'brokers'):
                pulled = subprocess.run(['docker', 'pull', '-q', manifest['images'][key]], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800, check=False)
                if pulled.returncode:
                    category = 'image_pull_failed'
                    return
            self.phase(record, 'backup')
            before = {folder.name for _, folder in src.backup_folders()}
            lock = os.open(src.control_root / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                code, lines = run_tool([str(src.recovery_script), 'backup', '--project-root', str(src.project_root),
                                        '--state-root', str(src.state_root), '--backup-root', str(src.backup_root),
                                        '--key-root', str(src.key_root), '--apply'], 4 * 3600)
            except BlockingIOError:
                category = 'operation_in_progress'
                return
            finally:
                os.close(lock)
            new = [folder for _, folder in src.backup_folders() if folder.name not in before]
            if code or 'backup=verified' not in lines or len(new) != 1:
                category = last_value(lines, 'failure_category') or 'backup_failed'
                return
            folder = new[0]
            record['backup'] = src.public_id('backup', folder.name)
            self.phase(record, 'update')
            code, lines = run_tool([str(src.update_script), '--project-root', str(src.project_root),
                                    '--state-root', str(src.state_root), '--release-manifest', str(manifest_path),
                                    '--backup-dir', str(folder), '--key-file', str(src.key_path(folder)),
                                    '--control-backup-root', str(src.control_root), UPDATE_MODES[src.update_mode],
                                    '--apply'], 3 * 3600)
            rollback = last_value(lines, 'rollback')
            if code == 0 and last_value(lines, 'release_update') == 'healthy':
                outcome = 'succeeded'
            else:
                category = last_value(lines, 'failure_category') or 'update_failed'
                outcome = {'healthy': 'rolled_back', 'failed_manual_recovery_needed': 'rollback_failed'}.get(
                    rollback, 'failed')
        except Exception:
            category = 'unexpected_error'
        finally:
            at = now_utc()
            record.update(outcome=outcome, phase='done', updated_utc=at, finished_utc=at,
                          failure_category=None if outcome == 'succeeded' else
                          (category if CODE.fullmatch(str(category or '')) else 'unexpected_error'),
                          rollback=rollback if rollback in ROLLBACKS else None)
            record['phases'].append({'phase': 'done', 'at': at})
            try:
                write_private(src.ops_job_path(record['job_id']), record)
            finally:
                src.running.release()


# ── Update candidates (separate, network-enabled timer unit) ──

RELEASES_API = 'https://api.github.com/repos/{repo}/releases?per_page=20'
TAG = re.compile(r'compose-([0-9a-f]{8})\Z')
PINNED = re.compile(r'[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z')
IMAGE_KEYS = {'host', 'agent', 'brokers', 'onecli', 'postgres', 'signal'}
MAX_MANIFEST = 16 * 1024


def valid_manifest(blob):
    """The release manifest shape the update tool accepts; it re-checks everything before applying."""
    if len(blob) > MAX_MANIFEST:
        return None
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    if not (isinstance(data, dict) and set(data) == {'schema', 'version', 'revision', 'tree', 'images'}
            and data['schema'] == 'nanoclaw-compose-release/v1'
            and isinstance(data['version'], str) and VERSION.fullmatch(data['version'])
            and isinstance(data['revision'], str) and REVISION.fullmatch(data['revision'])
            and isinstance(data['tree'], str) and REVISION.fullmatch(data['tree'])
            and isinstance(data['images'], dict) and set(data['images']) == IMAGE_KEYS
            and all(isinstance(ref, str) and PINNED.fullmatch(ref) for ref in data['images'].values())):
        return None
    return data


def as_owner(path):
    info = path.stat()

    def drop():
        os.setgroups([])
        os.setgid(info.st_gid)
        os.setuid(info.st_uid)
    return drop if os.geteuid() == 0 else None


def git(project, *args):
    env = os.environ.copy()
    if os.geteuid() == 0:
        import pwd
        account = pwd.getpwuid(project.stat().st_uid)
        env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
    return subprocess.run(['git', '-C', str(project), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, preexec_fn=as_owner(project), env=env, timeout=300, check=False)


def http_get(url, limit):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                   'User-Agent': 'nanoclaw-ops'})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('response too large')
    return body


def fetch_candidates(repo, candidates, state_root, project_root, get=http_get, keep=3):
    """Store the newest published manifests that descend from the installed release."""
    installed = private_json(Path(state_root) / 'release.json')
    if not isinstance(installed, dict) or not REVISION.fullmatch(str(installed.get('revision', ''))):
        print('candidates=installed_release_unknown', flush=True)
        return 1
    releases = json.loads(get(RELEASES_API.format(repo=repo), 1024 * 1024))
    project = Path(project_root).resolve()
    if git(project, 'fetch', '-q', 'origin', 'main').returncode:
        print('candidates=git_fetch_failed', flush=True)
        return 1
    stored = []
    for release in releases if isinstance(releases, list) else []:
        match = TAG.fullmatch(str(release.get('tag_name', ''))) if isinstance(release, dict) else None
        if not match or release.get('draft') or release.get('prerelease'):
            continue
        asset = next((a for a in release.get('assets', []) if isinstance(a, dict)
                      and a.get('name') == 'compose-release.json'), None)
        url = asset.get('browser_download_url') if asset else None
        if not isinstance(url, str) or not url.startswith('https://github.com/'):
            continue
        blob = get(url, MAX_MANIFEST)
        manifest = valid_manifest(blob)
        if not manifest or not manifest['revision'].startswith(match.group(1)):
            continue
        revision = manifest['revision']
        if revision == installed['revision']:
            break
        if git(project, 'merge-base', '--is-ancestor', installed['revision'], revision).returncode:
            continue  # not a descendant of what runs now
        path = Path(candidates) / f'{revision}.json'
        if not path.exists():
            write_private(path, manifest)
        stored.append(path)
        if len(stored) >= keep:
            break
    for path in Path(candidates).glob('*.json'):
        if path not in stored:
            path.unlink()
    print(f'candidates={len(stored)}', flush=True)
    return 0


def page(items, cursor):
    offset = int(cursor[1:]) if cursor else 0
    chunk = items[offset:offset + PAGE_SIZE]
    following = f'o{offset + PAGE_SIZE}' if offset + PAGE_SIZE < len(items) else None
    return {'items': chunk, 'next_cursor': following}


def confirmed(body):
    if not isinstance(body, dict) or set(body) != {'confirm'} or body['confirm'] is not True:
        raise OpsError(400, 'invalid_request')


def handle(sources, method, target, body=None):
    """Return (status, body) for one request; raises OpsError for refusals."""
    if not isinstance(target, str) or len(target) > 512 or not target.startswith('/'):
        raise OpsError(400, 'invalid_request')
    parts = urlsplit(target)
    query = parse_qsl(parts.query, keep_blank_values=True)
    path = parts.path
    key_match = re.fullmatch(r'/api/v1/backups/([^/]+)/key(/saved)?', path)
    job_match = re.fullmatch(r'/api/v1/jobs/([^/]+)', path)
    name = {'/api/v1/releases': 'releases', '/api/v1/backups': 'backups'}.get(path)
    if key_match:
        if method != 'POST':
            raise OpsError(405, 'method_not_allowed')
        if query:
            raise OpsError(400, 'invalid_query')
        confirmed(body)
        public = key_match.group(1)
        return 200, (sources.forget_key(public) if key_match.group(2) else sources.reveal_key(public))
    if path == '/api/v1/updates':
        if method != 'POST':
            raise OpsError(405, 'method_not_allowed')
        if query:
            raise OpsError(400, 'invalid_query')
        if not isinstance(body, dict) or set(body) != {'release_revision', 'confirm'} or body['confirm'] is not True \
                or not isinstance(body['release_revision'], str) or not REVISION.fullmatch(body['release_revision']):
            raise OpsError(400, 'invalid_request')
        return 202, UpdateRunner(sources).start(body['release_revision'])
    if name is None and not job_match:
        if path.startswith('/api/v1/backups/') or path == '/api/v1/imports/preflight':
            raise OpsError(501, 'not_implemented')
        raise OpsError(404, 'not_found')
    if name == 'backups' and method == 'POST':
        if query:
            raise OpsError(400, 'invalid_query')
        confirmed(body)
        return 202, sources.start_backup()
    if method != 'GET':
        raise OpsError(405, 'method_not_allowed')
    if body is not None:
        raise OpsError(400, 'invalid_request')
    cursor = None
    if query:
        if name != 'backups' or len(query) != 1 or query[0][0] != 'cursor' or not CURSOR.fullmatch(query[0][1]):
            raise OpsError(400, 'invalid_query')
        cursor = query[0][1]
    if job_match:
        job_id = job_match.group(1)
        if not JOB_ID.fullmatch(job_id):
            raise OpsError(404, 'not_found')
        found = sources.find_job(job_id)
        if not found:
            raise OpsError(404, 'not_found')
        return 200, found
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
                payload = None
                if length:
                    try:
                        payload = json.loads(self.rfile.read(length))
                    except ValueError:
                        raise OpsError(400, 'invalid_request')
                status, body = handle(sources, self.command, self.path, payload)
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
    here = Path(__file__).resolve().parent
    sources = Sources(args.state_root, args.backup_root, args.control_backup_root, args.candidates, key,
                      key_root=args.key_root, project_root=args.project_root, jobs_dir=args.jobs_dir,
                      recovery_script=here / 'compose-recovery.py', update_script=here / 'compose-release-update.py',
                      update_mode=args.update_mode)
    if args.jobs_dir:
        os.makedirs(args.jobs_dir, mode=0o700, exist_ok=True)
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
    s.add_argument('--key-root', default=None, help='backup keys; enables backups from the panel')
    s.add_argument('--project-root', default=None, help='the Compose checkout; enables backups from the panel')
    s.add_argument('--jobs-dir', default=None, help='private directory for job records')
    s.add_argument('--update-mode', choices=sorted(UPDATE_MODES), default=None,
                   help='enables updates from the panel; production for real identities, synthetic for test hosts')
    f = sub.add_parser('fetch-candidates')
    f.add_argument('--repo', required=True)
    f.add_argument('--candidates', required=True)
    f.add_argument('--state-root', default='/srv/nanoclaw')
    f.add_argument('--project-root', required=True)
    args = parser.parse_args(argv)
    if args.command == 'fetch-candidates':
        os.umask(0o077)
        sys.exit(fetch_candidates(args.repo, args.candidates, args.state_root, args.project_root))
    serve(args)


if __name__ == '__main__':
    main()
