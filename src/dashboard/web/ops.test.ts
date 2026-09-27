/**
 * Cross-language check: the Python operations service (scripts/compose-ops.py)
 * behind the dashboard. Every answer passes the dashboard's own contract
 * validation, so a 200 here means the Python projection fits the TypeScript
 * contract.
 */
import { spawn, type ChildProcess } from 'child_process';
import fs from 'fs';
import http from 'http';
import type { AddressInfo } from 'net';
import os from 'os';
import path from 'path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import { backupList, job, releases } from '../contract/api.js';
import { validate } from '../contract/schema.js';
import { findLeaks } from '../fixtures/canaries.js';
import { hashPassword } from './password.js';
import { createDashboardServer, socketForward } from './server.js';
import { DashboardState } from './state.js';

const FAST = { N: 2 ** 14, r: 8, p: 1 };
const PASSWORD = 'correct horse battery staple';
const ORIGIN = 'https://panel.example.invalid';
const OLD = 'a'.repeat(40);
const NEW = 'c'.repeat(40);

function writePrivate(file: string, value: unknown): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, typeof value === 'string' ? value : JSON.stringify(value), { mode: 0o600 });
}

describe('operations service behind the dashboard', () => {
  let root: string;
  let python: ChildProcess;
  let server: http.Server;
  let port: number;
  let cookie = '';

  beforeAll(async () => {
    root = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-ops-'));
    writePrivate(path.join(root, 'state/release.json'), { version: '2.4.0', revision: NEW, tree: 'd'.repeat(40) });
    writePrivate(path.join(root, 'control/compose-release-update.job.json'), {
      schema: 'nanoclaw-compose-release-job/v1',
      job_id: '0123456789abcdef',
      mode: 'production',
      apply: true,
      from_revision: OLD,
      to_revision: NEW,
      to_version: '2.4.0',
      phase: 'refresh_dashboard',
      outcome: 'succeeded',
      failure_category: null,
      rollback: null,
      started_utc: '2026-01-15T12:00:00Z',
      updated_utc: '2026-01-15T12:04:00Z',
      finished_utc: '2026-01-15T12:04:00Z',
      phases: [{ phase: 'preflight', at: '2026-01-15T12:00:00Z' }],
    });
    const folder = path.join(root, 'backups/aaaaaaaa-20260115T120000Z-000001');
    writePrivate(path.join(folder, 'manifest.json'), {
      revision: OLD,
      created_utc: '20260115T120000Z',
      hmac_sha256: 'x',
    });
    writePrivate(path.join(folder, 'state.tar.enc'), 'x'.repeat(64));
    writePrivate(path.join(root, 'id-key'), Buffer.alloc(32, 7).toString('latin1'));

    const socket = path.join(root, 'ops.sock');
    const driver = `
import importlib.util, sys
spec = importlib.util.spec_from_file_location('ops', sys.argv[1]); ops = importlib.util.module_from_spec(spec); spec.loader.exec_module(ops)
r = sys.argv[2]
sources = ops.Sources(r + '/state', r + '/backups', r + '/control', None, bytes([7]) * 32)
server = ops.UnixServer(sys.argv[3], ops.make_handler(sources)); print('ready', flush=True); server.serve_forever()
`;
    python = spawn('python3', ['-c', driver, path.resolve('scripts/compose-ops.py'), root, socket], {
      stdio: ['ignore', 'pipe', 'inherit'],
    });
    await new Promise<void>((resolve) => python.stdout!.once('data', () => resolve()));

    const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-ops-state-'));
    fs.chmodSync(stateDir, 0o700);
    const state = new DashboardState(stateDir);
    state.setPassword(await hashPassword(PASSWORD, FAST), new Date());
    server = createDashboardServer({
      state,
      origin: ORIGIN,
      forward: async () => ({
        status: 501,
        body: { error: { code: 'not_implemented', request_id: 'req_0000000000000000' } },
      }),
      opsForward: socketForward(socket),
      scrypt: FAST,
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    port = (server.address() as AddressInfo).port;
    const login = await call('POST', '/api/v1/session', { password: PASSWORD }, { origin: ORIGIN });
    cookie = String(login.headers['set-cookie']).split(';')[0];
  });

  afterAll(async () => {
    python?.kill();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    fs.rmSync(root, { recursive: true, force: true });
  });

  function call(method: string, target: string, body?: unknown, headers: Record<string, string> = {}) {
    return new Promise<{ status: number; headers: http.IncomingHttpHeaders; text: string }>((resolve, reject) => {
      const payload = body === undefined ? undefined : JSON.stringify(body);
      const request = http.request(
        {
          host: '127.0.0.1',
          port,
          method,
          path: target,
          headers: {
            ...(payload ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) } : {}),
            ...(cookie ? { cookie } : {}),
            ...headers,
          },
        },
        (response) => {
          const chunks: Buffer[] = [];
          response.on('data', (chunk: Buffer) => chunks.push(chunk));
          response.on('end', () =>
            resolve({
              status: response.statusCode!,
              headers: response.headers,
              text: Buffer.concat(chunks).toString(),
            }),
          );
        },
      );
      request.on('error', reject);
      if (payload) request.write(payload);
      request.end();
    });
  }

  it('serves releases, backups and the update job within the contract', async () => {
    const release = await call('GET', '/api/v1/releases');
    expect(release.status).toBe(200);
    const releaseBody = JSON.parse(release.text);
    expect(validate(releases, releaseBody)).toEqual([]);
    expect(releaseBody.last_update.outcome).toBe('succeeded');

    const backups = await call('GET', '/api/v1/backups');
    expect(backups.status).toBe(200);
    const backupBody = JSON.parse(backups.text);
    expect(validate(backupList, backupBody)).toEqual([]);
    expect(backupBody.items).toHaveLength(1);

    const found = await call('GET', `/api/v1/jobs/${releaseBody.last_update.id}`);
    expect(found.status).toBe(200);
    expect(validate(job, JSON.parse(found.text))).toEqual([]);

    for (const text of [release.text, backups.text, found.text]) {
      expect(findLeaks(text)).toEqual([]);
      expect(text).not.toContain(root);
      expect(text).not.toContain('aaaaaaaa-20260115');
    }
  });

  it('keeps operations the service does not run yet as not_implemented', async () => {
    const session = JSON.parse((await call('GET', '/api/v1/session')).text);
    await call(
      'POST',
      '/api/v1/session/reauth',
      { password: PASSWORD },
      { origin: ORIGIN, 'x-csrf-token': session.csrf_token },
    );
    const update = await call(
      'POST',
      '/api/v1/updates',
      { release_revision: NEW, confirm: true },
      { origin: ORIGIN, 'x-csrf-token': session.csrf_token },
    );
    expect(update.status).toBe(501);
    expect(JSON.parse(update.text).error.code).toBe('not_implemented');
  });
});

describe('dashboard without the operations service', () => {
  it('reports ops_unavailable instead of a generic error', async () => {
    const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-noops-'));
    fs.chmodSync(stateDir, 0o700);
    const state = new DashboardState(stateDir);
    state.setPassword(await hashPassword(PASSWORD, FAST), new Date());
    const server = createDashboardServer({
      state,
      origin: ORIGIN,
      forward: async () => ({ status: 200, body: {} }),
      opsForward: socketForward(path.join(stateDir, 'missing.sock')),
      scrypt: FAST,
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    const port = (server.address() as AddressInfo).port;
    const request = (method: string, target: string, body?: unknown, headers: Record<string, string> = {}) =>
      new Promise<{ status: number; headers: http.IncomingHttpHeaders; text: string }>((resolve) => {
        const payload = body === undefined ? undefined : JSON.stringify(body);
        const req = http.request(
          {
            host: '127.0.0.1',
            port,
            method,
            path: target,
            headers: { ...(payload ? { 'content-type': 'application/json' } : {}), ...headers },
          },
          (response) => {
            const chunks: Buffer[] = [];
            response.on('data', (chunk: Buffer) => chunks.push(chunk));
            response.on('end', () =>
              resolve({
                status: response.statusCode!,
                headers: response.headers,
                text: Buffer.concat(chunks).toString(),
              }),
            );
          },
        );
        if (payload) req.write(payload);
        req.end();
      });
    const login = await request('POST', '/api/v1/session', { password: PASSWORD }, { origin: ORIGIN });
    const cookie = String(login.headers['set-cookie']).split(';')[0];
    const backups = await request('GET', '/api/v1/backups', undefined, { cookie });
    expect(backups.status).toBe(503);
    expect(JSON.parse(backups.text).error.code).toBe('ops_unavailable');
    await new Promise<void>((resolve) => server.close(() => resolve()));
    fs.rmSync(stateDir, { recursive: true, force: true });
  });
});
