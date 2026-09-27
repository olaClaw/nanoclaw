import { spawnSync } from 'child_process';
import fs from 'fs';
import http from 'http';
import type { AddressInfo } from 'net';
import os from 'os';
import path from 'path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { closeDb, initTestDb } from '../../db/connection.js';
import { runMigrations } from '../../db/migrations/index.js';
import { sessionState } from '../contract/api.js';
import { validate } from '../contract/schema.js';
import { findLeaks } from '../fixtures/canaries.js';
import { seedSyntheticInstall } from '../fixtures/synthetic-install.js';
import { startDashboardAdminSocket, stopDashboardAdminSocket } from '../host/admin-socket.js';
import type { HostSources } from '../host/projections.js';
import { runAdminCommand } from './admin-cli.js';
import { dashboardConfigFromEnv } from './main.js';
import { hashPassword, needsRehash, passwordProblem, verifyPassword, type ScryptParams } from './password.js';
import { COOKIE, OPS_ENDPOINTS, createDashboardServer, socketForward, type Forward } from './server.js';
import { ABSOLUTE_MS, IDLE_MS, REAUTH_MS, SessionStore } from './sessions.js';
import { DashboardState } from './state.js';
import { APP_JS, INDEX_HTML } from './ui.js';

const FAST: ScryptParams = { N: 2 ** 14, r: 8, p: 1 };
const PASSWORD = 'correct horse battery staple';
const ORIGIN = 'https://panel.example.invalid';

function privateDir(prefix: string): string {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  fs.chmodSync(directory, 0o700);
  return directory;
}

describe('operations routing', () => {
  it('sends every endpoint the operations service owns to it, and nothing else', () => {
    expect([...OPS_ENDPOINTS].sort()).toEqual(
      [
        'backup_create',
        'backup_export',
        'backup_key',
        'backup_key_saved',
        'backup_verify',
        'backups',
        'import_apply',
        'import_preflight',
        'job',
        'releases',
        'update',
      ].sort(),
    );
  });
});

describe('password hashing', () => {
  it('stores a versioned scrypt record and verifies only the right password', async () => {
    const stored = await hashPassword(PASSWORD, FAST);
    expect(stored).toMatch(/^scrypt\$1\$16384\$8\$1\$[A-Za-z0-9_-]+\$[A-Za-z0-9_-]{43}$/);
    expect(stored).not.toContain(PASSWORD);
    expect(await verifyPassword(PASSWORD, stored)).toBe(true);
    expect(await verifyPassword(PASSWORD + ' ', stored)).toBe(false);
    expect(await hashPassword(PASSWORD, FAST)).not.toBe(stored);
  });

  it('fails closed on a missing or malformed record', async () => {
    expect(await verifyPassword(PASSWORD, null)).toBe(false);
    expect(await verifyPassword(PASSWORD, 'scrypt$1$3$8$1$AAAA$BBBB')).toBe(false);
    expect(await verifyPassword(PASSWORD, 'plaintext')).toBe(false);
  });

  it('normalizes Unicode, bounds length and flags weak parameters', async () => {
    const stored = await hashPassword('ﬁancé-passphrase', FAST);
    expect(await verifyPassword('fiancé-passphrase', stored)).toBe(true);
    expect(passwordProblem('short')).toBe('too_short');
    expect(passwordProblem('x'.repeat(1025))).toBe('too_long');
    expect(needsRehash(stored)).toBe(true);
    expect(needsRehash(stored, FAST)).toBe(false);
    await expect(hashPassword('short', FAST)).rejects.toThrow('too_short');
  });
});

describe('sessions', () => {
  it('expire on idle and absolute limits, revoke on generation change, bound the reauth window', () => {
    let now = 1_000_000;
    const store = new SessionStore(() => now);
    const { token, session } = store.create(1);
    expect(store.get(token, 1)).toBe(session);
    expect(store.get(token, 2)).toBeNull();

    const second = store.create(1);
    now += IDLE_MS + 1;
    expect(store.get(second.token, 1)).toBeNull();

    const third = store.create(1);
    for (let step = 0; step < ABSOLUTE_MS / (IDLE_MS / 2) + 1; step++) {
      now += IDLE_MS / 2;
      store.get(third.token, 1);
    }
    expect(store.get(third.token, 1)).toBeNull();

    const fourth = store.create(1);
    store.markReauth(fourth.session);
    expect(store.hasReauth(fourth.session)).toBe(true);
    now += REAUTH_MS + 1;
    expect(store.hasReauth(fourth.session)).toBe(false);
    expect(store.get('not a token', 1)).toBeNull();
  });
});

describe('state directory and local commands', () => {
  it('refuses an open directory and keeps files private', async () => {
    const open = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-open-'));
    fs.chmodSync(open, 0o755);
    expect(() => new DashboardState(open)).toThrow('private');
    const directory = privateDir('dash-state-');
    const state = new DashboardState(directory);
    state.setPassword(await hashPassword(PASSWORD, FAST), new Date());
    expect(fs.statSync(path.join(directory, 'admin.json')).mode & 0o777).toBe(0o600);
    expect(state.admin()!.generation).toBe(1);
    expect(await runAdminCommand('revoke-sessions', state)).toMatch(/revoked/);
    expect(state.admin()!.generation).toBe(2);
    state.setThrottle({ failures: 9, locked_until: new Date(Date.now() + 60_000).toISOString() });
    await runAdminCommand('unlock', state);
    expect(state.throttle()).toEqual({ failures: 0, locked_until: null });
    await expect(runAdminCommand('nope', state)).rejects.toThrow('usage');
  });

  it('sets the password from stdin without echoing it', () => {
    const directory = privateDir('dash-cli-');
    const result = spawnSync(process.execPath, ['--import', 'tsx', 'src/dashboard/web/admin-cli.ts', 'set-password'], {
      input: `${PASSWORD}\n`,
      env: { ...process.env, NANOCLAW_DASHBOARD_STATE_DIR: directory },
      encoding: 'utf8',
    });
    expect(result.stdout + result.stderr).not.toContain(PASSWORD);
    expect(result.stdout).toContain('password set');
    expect(new DashboardState(directory).admin()!.password).toMatch(/^scrypt\$1\$131072\$/);
  });

  it('requires an https origin and the private paths', () => {
    expect(() => dashboardConfigFromEnv({ NANOCLAW_DASHBOARD_ORIGIN: 'http://panel.example.invalid' })).toThrow(
      'https',
    );
    expect(() => dashboardConfigFromEnv({ NANOCLAW_DASHBOARD_ORIGIN: `${ORIGIN}/x` })).toThrow('https');
    expect(() => dashboardConfigFromEnv({ NANOCLAW_DASHBOARD_ORIGIN: ORIGIN })).toThrow('STATE_DIR');
  });
});

interface Response {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: string;
  json: () => unknown;
}

describe('dashboard server end to end over the host boundary', () => {
  let stateDir: string;
  let socketDir: string;
  let state: DashboardState;
  let server: http.Server;
  let port: number;
  let clock: number;

  const sources: HostSources = {
    idKey: Buffer.alloc(32, 7),
    channels: () => [{ key: 'cli', channelType: 'cli', connected: true }],
    release: () => ({ version: '2.4.0', revision: 'a'.repeat(40) }),
    defaults: { provider: 'opencode', model: 'fixture-model-a', endpointConfigured: true },
    now: () => new Date('2026-01-15T12:00:00Z'),
  };

  async function start(forward?: Forward): Promise<void> {
    server = createDashboardServer({
      state,
      origin: ORIGIN,
      forward: forward ?? socketForward(path.join(socketDir, 'admin.sock')),
      now: () => clock,
      scrypt: FAST,
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    port = (server.address() as AddressInfo).port;
  }

  beforeEach(async () => {
    clock = Date.parse('2026-01-15T12:00:00Z');
    await runMigrations(await initTestDb());
    await seedSyntheticInstall();
    socketDir = privateDir('dash-sock-');
    fs.chmodSync(socketDir, 0o750);
    await startDashboardAdminSocket(path.join(socketDir, 'admin.sock'), sources);
    stateDir = privateDir('dash-web-');
    state = new DashboardState(stateDir);
    state.setPassword(await hashPassword(PASSWORD, FAST), new Date());
    await start();
  });

  afterEach(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
    await stopDashboardAdminSocket();
    await closeDb();
    fs.rmSync(stateDir, { recursive: true, force: true });
    fs.rmSync(socketDir, { recursive: true, force: true });
  });

  function call(method: string, target: string, options: { body?: unknown; headers?: Record<string, string> } = {}) {
    return new Promise<Response>((resolve, reject) => {
      const payload = options.body === undefined ? undefined : JSON.stringify(options.body);
      const request = http.request(
        {
          host: '127.0.0.1',
          port,
          method,
          path: target,
          headers: {
            ...(payload === undefined
              ? {}
              : { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) }),
            ...options.headers,
          },
        },
        (response) => {
          const chunks: Buffer[] = [];
          response.on('data', (chunk: Buffer) => chunks.push(chunk));
          response.on('end', () => {
            const body = Buffer.concat(chunks).toString();
            resolve({ status: response.statusCode!, headers: response.headers, body, json: () => JSON.parse(body) });
          });
        },
      );
      request.on('error', reject);
      if (payload !== undefined) request.write(payload);
      request.end();
    });
  }

  async function login(password = PASSWORD): Promise<{ cookie: string; csrf: string; response: Response }> {
    const response = await call('POST', '/api/v1/session', { body: { password }, headers: { origin: ORIGIN } });
    const cookie = String(response.headers['set-cookie'] ?? '').split(';')[0];
    const csrf = response.status === 200 ? (response.json() as { csrf_token: string }).csrf_token : '';
    return { cookie, csrf, response };
  }

  it('serves the UI with a same-origin CSP and no inline code', async () => {
    const page = await call('GET', '/');
    expect(page.status).toBe(200);
    expect(page.headers['content-type']).toContain('text/html');
    const csp = String(page.headers['content-security-policy']);
    expect(csp).toContain("script-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toContain('unsafe-inline');
    expect(page.headers['cache-control']).toBe('no-store');
    for (const tag of page.body.match(/<script[^>]*>/g) ?? []) expect(tag).toMatch(/ src="\/app\.js"/);
    expect(page.body).not.toMatch(/<style|\sstyle=|\son[a-z]+=/i);
    const script = await call('GET', '/app.js');
    expect(script.headers['content-type']).toContain('text/javascript');
    expect((await call('GET', '/app.css')).headers['content-type']).toContain('text/css');
    expect((await call('GET', '/app.js?v=1')).status).toBe(404);
    expect((await call('GET', '/index.html')).status).toBe(404);
  });

  it('renders data as text only and keeps the CSRF token out of storage', () => {
    expect(() => new Function(APP_JS)).not.toThrow();
    for (const banned of [
      'innerHTML',
      'outerHTML',
      'insertAdjacentHTML',
      'document.write',
      'localStorage',
      'sessionStorage',
      'eval(',
      'new Function',
    ]) {
      expect(APP_JS, banned).not.toContain(banned);
    }
    for (const [, target] of APP_JS.matchAll(/fetch\(([^,]+),/g)) expect(target.trim()).toBe("'/api/v1' + path");
    expect(INDEX_HTML).not.toMatch(/https?:\/\//);
  });

  it('answers health anonymously with the security headers', async () => {
    const response = await call('GET', '/api/v1/health');
    expect(response.status).toBe(200);
    expect(response.json()).toEqual({ status: 'ok' });
    expect(response.headers['cache-control']).toBe('no-store');
    expect(response.headers['content-security-policy']).toContain("frame-ancestors 'none'");
    expect(response.headers['x-frame-options']).toBe('DENY');
    expect(response.headers['strict-transport-security']).toContain('max-age=');
  });

  it('requires a session for everything else', async () => {
    const response = await call('GET', '/api/v1/agents');
    expect(response.status).toBe(401);
    expect((response.json() as { error: { code: string } }).error.code).toBe('unauthenticated');
    expect((await call('GET', '/nope')).status).toBe(404);
  });

  it('logs in with a hardened cookie and serves the host boundary end to end', async () => {
    const { cookie, response } = await login();
    expect(response.status).toBe(200);
    expect(validate(sessionState, response.json())).toEqual([]);
    const setCookie = String(response.headers['set-cookie']);
    expect(setCookie).toMatch(new RegExp(`^${COOKIE}=[A-Za-z0-9_-]{43}; Path=/; Secure; HttpOnly; SameSite=Strict$`));

    const agents = await call('GET', '/api/v1/agents', { headers: { cookie } });
    expect(agents.status).toBe(200);
    expect((agents.json() as { items: unknown[] }).items.length).toBe(2);
    expect(findLeaks(agents.body)).toEqual([]);
  });

  it('refuses login from another origin and cross-site fetches', async () => {
    expect((await call('POST', '/api/v1/session', { body: { password: PASSWORD } })).status).toBe(403);
    expect(
      (
        await call('POST', '/api/v1/session', {
          body: { password: PASSWORD },
          headers: { origin: 'https://evil.example.invalid' },
        })
      ).status,
    ).toBe(403);
    const { cookie } = await login();
    expect((await call('GET', '/api/v1/agents', { headers: { cookie, 'sec-fetch-site': 'cross-site' } })).status).toBe(
      403,
    );
    expect(
      (await call('GET', '/api/v1/agents', { headers: { cookie, origin: 'https://evil.example.invalid' } })).status,
    ).toBe(403);
  });

  it('gives a uniform error for wrong passwords and throttles persistently', async () => {
    for (let attempt = 0; attempt < 5; attempt++) {
      const { response } = await login('wrong password number one');
      expect(response.status).toBe(401);
      expect((response.json() as { error: { code: string } }).error.code).toBe('invalid_credentials');
    }
    await login('wrong password number six');
    const locked = await login();
    expect(locked.response.status).toBe(429);
    expect(locked.response.headers['retry-after']).toBeDefined();
    // A restart does not reset the throttle.
    await new Promise<void>((resolve) => server.close(() => resolve()));
    await start();
    expect((await login()).response.status).toBe(429);
    clock += 61_000;
    expect((await login()).response.status).toBe(200);
  });

  it('checks CSRF on state changes and forwards them', async () => {
    const { cookie, csrf } = await login();
    const agents = (await call('GET', '/api/v1/agents', { headers: { cookie } })).json() as {
      items: Array<{ id: string }>;
    };
    const target = `/api/v1/agents/${agents.items[0].id}/restart`;
    const base = { cookie, origin: ORIGIN };
    expect((await call('POST', target, { body: { confirm: true }, headers: base })).status).toBe(403);
    expect(
      (await call('POST', target, { body: { confirm: true }, headers: { ...base, 'x-csrf-token': 'x'.repeat(43) } }))
        .status,
    ).toBe(403);
    const forwarded = await call('POST', target, {
      body: { confirm: true },
      headers: { ...base, 'x-csrf-token': csrf },
    });
    expect(forwarded.status).toBe(501);
    expect((forwarded.json() as { error: { code: string } }).error.code).toBe('not_implemented');
  });

  it('asks for the password again before dangerous operations', async () => {
    const { cookie, csrf } = await login();
    const headers = { cookie, origin: ORIGIN, 'x-csrf-token': csrf };
    const update = { release_revision: 'b'.repeat(40), confirm: true };
    expect((await call('POST', '/api/v1/updates', { body: update, headers })).status).toBe(403);
    expect(
      (await call('POST', '/api/v1/session/reauth', { body: { password: 'wrong password again' }, headers })).status,
    ).toBe(401);
    const reauth = await call('POST', '/api/v1/session/reauth', { body: { password: PASSWORD }, headers });
    expect((reauth.json() as { reauth_expires_at: string | null }).reauth_expires_at).not.toBeNull();
    expect((await call('POST', '/api/v1/updates', { body: update, headers })).status).toBe(501);
    clock += REAUTH_MS + 1;
    expect((await call('POST', '/api/v1/updates', { body: update, headers })).status).toBe(403);
  });

  it('logs out, and a password change revokes every session', async () => {
    const first = await login();
    const logout = await call('DELETE', '/api/v1/session', {
      headers: { cookie: first.cookie, origin: ORIGIN, 'x-csrf-token': first.csrf },
    });
    expect(logout.status).toBe(204);
    expect(String(logout.headers['set-cookie'])).toContain('Max-Age=0');
    expect((await call('GET', '/api/v1/session', { headers: { cookie: first.cookie } })).status).toBe(401);

    const second = await login();
    state.setPassword(await hashPassword('another long passphrase', FAST), new Date());
    expect((await call('GET', '/api/v1/session', { headers: { cookie: second.cookie } })).status).toBe(401);
  });

  it('expires an idle session', async () => {
    const { cookie } = await login();
    clock += IDLE_MS + 1;
    expect((await call('GET', '/api/v1/agents', { headers: { cookie } })).status).toBe(401);
  });

  it('refuses oversized and non-JSON bodies', async () => {
    const huge = await call('POST', '/api/v1/session', {
      body: { password: 'x'.repeat(70_000) },
      headers: { origin: ORIGIN },
    });
    expect(huge.status).toBe(413);
    const text = await new Promise<number>((resolve) => {
      const request = http.request(
        {
          host: '127.0.0.1',
          port,
          method: 'POST',
          path: '/api/v1/session',
          headers: { origin: ORIGIN, 'content-type': 'text/plain' },
        },
        (response) => {
          response.resume();
          resolve(response.statusCode!);
        },
      );
      request.on('error', () => resolve(-1));
      request.end('password');
    });
    expect(text).toBe(415);
  });

  it('drops an upstream answer that does not fit the contract', async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
    await start(async () => ({
      status: 200,
      body: { items: [{ id: 'ag-raw', platform_id: '+15550100173' }], next_cursor: null },
    }));
    const { cookie } = await login();
    const response = await call('GET', '/api/v1/agents', { headers: { cookie } });
    expect(response.status).toBe(502);
    expect(findLeaks(response.body)).toEqual([]);
  });

  it('audits state changes with endpoint, status and request id only', async () => {
    await login('wrong password for audit');
    const { cookie, csrf } = await login();
    await call('DELETE', '/api/v1/session', { headers: { cookie, origin: ORIGIN, 'x-csrf-token': csrf } });
    const lines = fs
      .readFileSync(path.join(stateDir, 'audit.jsonl'), 'utf8')
      .trim()
      .split('\n')
      .map((line) => JSON.parse(line));
    expect(lines.map((line) => [line.endpoint, line.status])).toEqual([
      ['login', 401],
      ['login', 200],
      ['logout', 204],
    ]);
    for (const line of lines) expect(Object.keys(line).sort()).toEqual(['at', 'endpoint', 'request_id', 'status']);
    const text = JSON.stringify(lines);
    expect(text).not.toContain(PASSWORD);
    expect(text).not.toContain(cookie.split('=')[1]);
    expect(text).not.toContain(csrf);
  });
});
