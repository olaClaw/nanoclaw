/**
 * The dashboard service's own private state (D2): the administrator
 * credential, the login throttle and the audit log. It lives in a directory
 * only the dashboard can read (0700), separate from NanoClaw's `data/`.
 *
 * - `admin.json`: password hash and a session generation. Bumping the
 *   generation (password change, local reset) revokes every session.
 * - `throttle.json`: failed-login counter and lock time, so a restart does not
 *   reset the rate limit.
 * - `audit.jsonl`: one line per state-changing request: time, endpoint,
 *   status and request ID. Never bodies, paths, cookies or values.
 */
import fs from 'fs';
import path from 'path';

import { isPasswordRecord } from './password.js';

export interface AdminRecord {
  schema: 'nanoclaw-dashboard-admin/v1';
  password: string;
  generation: number;
  updated_at: string;
}

export interface SetupRecord {
  code: string;
  expires_at: string;
}

export interface ThrottleRecord {
  failures: number;
  locked_until: string | null;
}

const AUDIT_MAX_BYTES = 8 * 1024 * 1024;

export class DashboardState {
  constructor(readonly directory: string) {
    const info = fs.lstatSync(directory);
    if (!info.isDirectory() || info.mode & 0o077) {
      throw new Error('dashboard state directory must be private (mode 0700)');
    }
  }

  private file(name: string): string {
    return path.join(this.directory, name);
  }

  private readJson<T>(name: string): T | null {
    const file = this.file(name);
    let info: fs.Stats;
    try {
      info = fs.lstatSync(file);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
      throw error;
    }
    if (!info.isFile() || info.mode & 0o077 || info.size > 64 * 1024) {
      throw new Error(`dashboard state file ${name} is not private`);
    }
    return JSON.parse(fs.readFileSync(file, 'utf8')) as T;
  }

  private writeJson(name: string, value: unknown): void {
    const file = this.file(name);
    const temporary = `${file}.${process.pid}.tmp`;
    const fd = fs.openSync(temporary, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
    try {
      fs.writeSync(fd, JSON.stringify(value, null, 2) + '\n');
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(temporary, file);
  }

  admin(): AdminRecord | null {
    const record = this.readJson<AdminRecord>('admin.json');
    if (!record) return null;
    if (
      record.schema !== 'nanoclaw-dashboard-admin/v1' ||
      typeof record.password !== 'string' ||
      !isPasswordRecord(record.password) ||
      !Number.isSafeInteger(record.generation)
    ) {
      throw new Error('dashboard admin record is invalid');
    }
    return record;
  }

  /** Store a new hash and revoke every existing session. */
  setPassword(hash: string, now: Date): AdminRecord {
    const previous = this.admin();
    const record: AdminRecord = {
      schema: 'nanoclaw-dashboard-admin/v1',
      password: hash,
      generation: (previous?.generation ?? 0) + 1,
      updated_at: now.toISOString(),
    };
    this.writeJson('admin.json', record);
    return record;
  }

  /** Revoke every session without changing the password. */
  revokeSessions(now: Date): void {
    const previous = this.admin();
    if (!previous) return;
    this.writeJson('admin.json', { ...previous, generation: previous.generation + 1, updated_at: now.toISOString() });
  }

  /** The pending first-run setup code, or null when none or expired. */
  setupCode(now: Date): SetupRecord | null {
    const record = this.readJson<SetupRecord>('setup.json');
    if (!record || typeof record.code !== 'string' || !(Date.parse(record.expires_at) > now.getTime())) return null;
    return record;
  }

  /** Reuse the pending code or create one; refused once an administrator exists. */
  ensureSetupCode(now: Date, generate: () => string, lifetimeMs: number): SetupRecord {
    if (this.admin()) throw new Error('an administrator already exists');
    const current = this.setupCode(now);
    if (current) return current;
    const record = { code: generate(), expires_at: new Date(now.getTime() + lifetimeMs).toISOString() };
    this.writeJson('setup.json', record);
    return record;
  }

  clearSetupCode(): void {
    fs.rmSync(this.file('setup.json'), { force: true });
  }

  throttle(): ThrottleRecord {
    return this.readJson<ThrottleRecord>('throttle.json') ?? { failures: 0, locked_until: null };
  }

  setThrottle(record: ThrottleRecord): void {
    this.writeJson('throttle.json', record);
  }

  audit(entry: { at: string; endpoint: string; status: number; request_id: string }): void {
    const file = this.file('audit.jsonl');
    try {
      if (fs.statSync(file).size > AUDIT_MAX_BYTES) fs.renameSync(file, `${file}.1`);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    }
    fs.appendFileSync(file, JSON.stringify(entry) + '\n', { mode: 0o600 });
  }
}
