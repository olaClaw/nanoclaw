import fs from 'fs';
import http from 'http';
import os from 'os';
import path from 'path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { closeDb, getDb, initTestDb } from '../../db/connection.js';
import { runMigrations } from '../../db/migrations/index.js';
import { log } from '../../log.js';
import { ENDPOINTS, endpoint } from '../contract/api.js';
import { publicId } from '../contract/opaque-id.js';
import { validate } from '../contract/schema.js';
import { CANARIES, findLeaks } from '../fixtures/canaries.js';
import { EXPECTED, SYNTHETIC, seedSyntheticInstall } from '../fixtures/synthetic-install.js';
import { MAX_BODY_BYTES, startDashboardAdminSocket, stopDashboardAdminSocket } from './admin-socket.js';
import { DASHBOARD_LOCAL, handleAdminRequest } from './boundary.js';
import { loadIdKey } from './host-sources.js';
import { PAGE_SIZE, safeLabel, type HostSources } from './projections.js';

const KEY = Buffer.alloc(32, 7);

function sources(changes: Partial<HostSources> = {}): HostSources {
  return {
    idKey: KEY,
    channels: () => [
      { key: 'signal', channelType: 'signal', connected: true },
      { key: 'telegram', channelType: 'telegram', connected: false },
      { key: 'cli', channelType: 'cli', connected: true },
    ],
    release: () => ({ version: '2.4.0', revision: 'a'.repeat(40) }),
    defaults: { provider: 'opencode', model: 'fixture-model-a', opencodeModel: '', endpointConfigured: true },
    now: () => new Date('2026-01-15T12:34:56.789Z'),
    restartAgent: async () => 1,
    dataDir: os.tmpdir(),
    probeModel: async () => ({ reason: null, models: [], contextLimit: null }),
    endpointState: async () => 'reachable',
    ...changes,
  };
}

const get = (target: string, host = sources()) => handleAdminRequest({ method: 'GET', target, body: undefined }, host);
const mainId = publicId('agent', SYNTHETIC.agents.main.id, KEY);

beforeEach(async () => {
  await runMigrations(await initTestDb());
  await seedSyntheticInstall();
});
afterEach(async () => {
  await closeDb();
});

describe('read-only projections over the synthetic install', () => {
  const targets = [
    '/api/v1/overview',
    '/api/v1/agents',
    `/api/v1/agents/${'{main}'}`,
    '/api/v1/channels',
    '/api/v1/sessions',
    '/api/v1/model-settings',
  ];

  it('return contract-valid bodies with no canary, address, URL or path', async () => {
    for (const raw of targets) {
      const target = raw.replace('{main}', mainId);
      const response = await get(target);
      expect(response.status, target).toBe(200);
      const schema = endpoint(response.endpoint!).response!;
      expect(validate(schema, response.body), target).toEqual([]);
      expect(findLeaks(JSON.stringify(response.body)), target).toEqual([]);
    }
  });

  it('count agents, sessions and chats like the seed', async () => {
    const agents = (await get('/api/v1/agents')).body as {
      items: Array<{ id: string; label: string; sessions: unknown; state: string }>;
    };
    expect(agents.items.length).toBe(EXPECTED.agents);
    const main = agents.items.find((item) => item.id === mainId)!;
    expect(main.label).toBe(SYNTHETIC.agents.main.label);
    expect(main.sessions).toEqual(EXPECTED.sessions.main);
    expect(main.state).toBe('running');

    const channels = (await get('/api/v1/channels')).body as {
      items: Array<{ type: string; chats: number; state: string }>;
    };
    expect(Object.fromEntries(channels.items.map((item) => [item.type, item.chats]))).toEqual(EXPECTED.channels);
    expect(channels.items.find((item) => item.type === 'telegram')!.state).toBe('disconnected');

    const sessions = (await get('/api/v1/sessions')).body as { items: Array<{ last_activity: string | null }> };
    expect(sessions.items.length).toBe(EXPECTED.sessions.total);
    expect(sessions.items[0].last_activity).toBe('2026-01-15T10:17:00Z');
  });

  it('report the agent detail as counts, never the configuration', async () => {
    const detail = (await get(`/api/v1/agents/${mainId}`)).body as {
      capabilities: unknown;
      container: { image: { derived: boolean } };
    };
    expect(detail.capabilities).toEqual({
      cli_scope: 'global',
      skills: 'selected',
      packages: 2,
      mcp_servers: 1,
      additional_mounts: 1,
    });
    expect(detail.container.image.derived).toBe(true);
  });

  it('see mixed model settings when agents disagree, uniform ones when they follow the default', async () => {
    const mixed = sources({
      defaults: { provider: 'claude', model: '', opencodeModel: '', endpointConfigured: false },
    });
    expect((await get('/api/v1/model-settings', mixed)).body).toMatchObject({
      mode: 'mixed',
      agents: { total: 2, matching: 1 },
      endpoint_status: { configured: false, state: 'unknown' },
    });
    // A config row without a provider runs on Claude, as at spawn: still mixed.
    expect((await get('/api/v1/model-settings')).body).toMatchObject({
      mode: 'mixed',
      agents: { total: 2, matching: 1 },
      endpoint_status: { configured: true, state: 'reachable' },
    });
    // Once the helper is on OpenCode it follows the install default model.
    await getDb().run(
      "UPDATE container_configs SET provider = 'opencode' WHERE agent_group_id = ?",
      SYNTHETIC.agents.helper.id,
    );
    expect((await get('/api/v1/model-settings')).body).toMatchObject({
      mode: 'local',
      model: 'fixture-model-a',
      agents: { total: 2, matching: 2 },
    });
    // With no install default, OpenCode groups fall back to OpenCode's own model.
    const opencodeOnly = sources({
      defaults: { provider: 'opencode', model: '', opencodeModel: 'openai/fixture-model-a', endpointConfigured: true },
    });
    await getDb().run('UPDATE container_configs SET model = NULL');
    expect((await get('/api/v1/model-settings', opencodeOnly)).body).toMatchObject({
      model: 'openai/fixture-model-a',
      agents: { total: 2, matching: 2 },
    });
  });

  it('page long lists with a cursor', async () => {
    const db = getDb();
    for (let index = 0; index < PAGE_SIZE + 5; index++) {
      await db.run(
        "INSERT INTO sessions (id, agent_group_id, messaging_group_id, thread_id, agent_provider, status, container_status, last_active, created_at) VALUES (?, ?, NULL, NULL, NULL, 'closed', 'stopped', NULL, '2026-01-15T09:00:00.000Z')",
        `sess-page-${index}`,
        SYNTHETIC.agents.helper.id,
      );
    }
    const first = (await get('/api/v1/sessions')).body as { items: unknown[]; next_cursor: string };
    expect(first.items.length).toBe(PAGE_SIZE);
    const second = (await get(`/api/v1/sessions?cursor=${first.next_cursor}`)).body as {
      items: unknown[];
      next_cursor: null;
    };
    expect(second.items.length).toBe(EXPECTED.sessions.total + 5);
    expect(second.next_cursor).toBeNull();
  });

  it('neutralize hostile labels and identifiers from the DB', async () => {
    await getDb().run(
      'UPDATE agent_groups SET name = ? WHERE id = ?',
      `‮${'x'.repeat(80)}\n${CANARIES.token}`,
      SYNTHETIC.agents.helper.id,
    );
    await getDb().run(
      'UPDATE container_configs SET provider = ?, model = ? WHERE agent_group_id = ?',
      'Bad Provider!',
      `${CANARIES.mcp_url}`,
      SYNTHETIC.agents.helper.id,
    );
    const agents = (await get('/api/v1/agents')).body as {
      items: Array<{ label: string; provider: string | null; model: string | null }>;
    };
    const helper = agents.items.find((item) => item.label.startsWith('x'))!;
    expect(helper.label.length).toBeLessThanOrEqual(64);
    expect(helper.provider).toBeNull();
    expect(helper.model).toBeNull();
    expect(findLeaks(JSON.stringify(agents))).toEqual([]);
    expect(safeLabel('\u0000​')).toBe('Agent');
  });
});

describe('agent restart', () => {
  it('restarts the resolved internal agent and answers within the contract', async () => {
    const calls: string[] = [];
    const host = sources({ restartAgent: async (id) => (calls.push(id), 2) });
    const target = endpoint('restart_agent').path.replace('{agent}', mainId);
    const response = await handleAdminRequest({ method: 'POST', target, body: { confirm: true } }, host);
    expect(response.status).toBe(200);
    expect(response.body).toEqual({ agent: mainId, restarted: 2 });
    expect(validate(endpoint('restart_agent').response!, response.body)).toEqual([]);
    expect(calls).toEqual([SYNTHETIC.agents.main.id]);
    expect(findLeaks(JSON.stringify(response.body))).toEqual([]);
  });

  it('refuses unknown agents and a second restart of the same agent while one runs', async () => {
    const unknown = endpoint('restart_agent').path.replace('{agent}', 'agt_' + '0'.repeat(32));
    expect(
      (await handleAdminRequest({ method: 'POST', target: unknown, body: { confirm: true } }, sources())).status,
    ).toBe(404);
    let release!: () => void;
    const slow = sources({ restartAgent: () => new Promise((resolve) => (release = () => resolve(1))) });
    const target = endpoint('restart_agent').path.replace('{agent}', mainId);
    const first = handleAdminRequest({ method: 'POST', target, body: { confirm: true } }, slow);
    await new Promise((resolve) => setTimeout(resolve, 10));
    const second = await handleAdminRequest({ method: 'POST', target, body: { confirm: true } }, slow);
    expect((second.body as { error: { code: string } }).error.code).toBe('restart_in_progress');
    release();
    expect((await first).status).toBe(200);
  });

  it('reports a failed restart as an error without detail', async () => {
    const failing = sources({
      restartAgent: async () => {
        throw new Error('docker said /srv/nanoclaw/secret');
      },
    });
    const target = endpoint('restart_agent').path.replace('{agent}', mainId);
    const response = await handleAdminRequest({ method: 'POST', target, body: { confirm: true } }, failing);
    expect(response.status).toBe(500);
    expect(findLeaks(JSON.stringify(response.body))).toEqual([]);
  });
});

describe('boundary rules', () => {
  it('refuses unknown routes, wrong methods and dashboard-local endpoints', async () => {
    expect((await get('/api/v1/nope')).status).toBe(404);
    expect(
      (await handleAdminRequest({ method: 'DELETE', target: '/api/v1/agents', body: undefined }, sources())).status,
    ).toBe(405);
    for (const name of DASHBOARD_LOCAL) {
      const local = endpoint(name);
      const response = await handleAdminRequest(
        { method: local.method, target: local.path, body: undefined },
        sources(),
      );
      expect(response.status, name).toBe(404);
    }
  });

  it('treats malformed or foreign ids as not found, never as a lookup key', async () => {
    for (const id of [
      SYNTHETIC.agents.main.id,
      'agt_' + '0'.repeat(32),
      publicId('agent', SYNTHETIC.agents.main.id, Buffer.alloc(32, 9)),
      '..%2f..%2fetc',
      "agt_' OR 1=1 --",
    ]) {
      const response = await get(`/api/v1/agents/${encodeURIComponent(id)}`);
      expect(response.status, id).toBe(404);
      expect(response.body).toEqual({ error: { code: 'not_found', request_id: response.requestId } });
    }
  });

  it('rejects unexpected query parameters and malformed cursors', async () => {
    expect((await get('/api/v1/overview?cursor=o50')).status).toBe(400);
    expect((await get('/api/v1/agents?cursor=o1&x=1')).status).toBe(400);
    expect((await get('/api/v1/agents?cursor=%27')).status).toBe(400);
    expect((await get('/api/v1/agents?limit=1000')).status).toBe(400);
  });

  it('validates request bodies before any handler runs', async () => {
    const restart = endpoint('restart_agent');
    const target = restart.path.replace('{agent}', mainId);
    expect((await handleAdminRequest({ method: 'POST', target, body: { confirm: false } }, sources())).status).toBe(
      400,
    );
    expect(
      (await handleAdminRequest({ method: 'POST', target, body: { confirm: true, extra: 1 } }, sources())).status,
    ).toBe(400);
    expect((await handleAdminRequest({ method: 'POST', target, body: { confirm: true } }, sources())).status).toBe(200);
    expect(
      (await handleAdminRequest({ method: 'GET', target: '/api/v1/agents', body: { a: 1 } }, sources())).status,
    ).toBe(400);
  });

  it('drops a projection result that does not fit the contract', async () => {
    const broken = sources({ release: () => ({ version: '2.4.0', revision: 'not-a-revision' }) });
    expect((await get('/api/v1/overview', broken)).status).toBe(503);
    const warn = vi.spyOn(log, 'warn').mockImplementation(() => {});
    const leaking = sources({ channels: () => [{ key: 'signal', channelType: CANARIES.phone, connected: true }] });
    const response = await get('/api/v1/channels', leaking);
    expect(response.status).toBe(200);
    expect(findLeaks(JSON.stringify(response.body))).toEqual([]);
    warn.mockRestore();
  });

  it('answers every contract endpoint with a contract-shaped body or error', async () => {
    for (const item of ENDPOINTS) {
      const target = item.path.replace(/\{([a-z]+)\}/g, (_, name: string) => (name === 'agent' ? mainId : 'x'));
      const response = await handleAdminRequest({ method: item.method, target, body: undefined }, sources());
      if (response.status < 300)
        expect(validate(item.response ?? { kind: 'boolean' }, response.body ?? true), item.name).toEqual([]);
      else
        expect(response.body).toEqual({
          error: { code: expect.stringMatching(/^[a-z_]+$/), request_id: response.requestId },
        });
    }
  });

  it('never logs paths, ids or values', async () => {
    const warn = vi.spyOn(log, 'warn').mockImplementation(() => {});
    const info = vi.spyOn(log, 'info').mockImplementation(() => {});
    await get(`/api/v1/agents/${mainId}`);
    await get('/api/v1/overview', sources({ release: () => null }));
    const logged = JSON.stringify([...warn.mock.calls, ...info.mock.calls]);
    expect(logged).not.toContain(mainId);
    expect(findLeaks(logged)).toEqual([]);
    warn.mockRestore();
    info.mockRestore();
  });
});

describe('admin socket', () => {
  let directory: string;
  let socket: string;

  beforeEach(async () => {
    directory = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-admin-'));
    fs.chmodSync(directory, 0o750);
    socket = path.join(directory, 'admin.sock');
    await startDashboardAdminSocket(socket, sources());
  });
  afterEach(async () => {
    await stopDashboardAdminSocket();
    fs.rmSync(directory, { recursive: true, force: true });
  });

  function call(method: string, target: string, body?: string, headers: Record<string, string> = {}) {
    return new Promise<{ status: number; headers: http.IncomingHttpHeaders; body: string }>((resolve, reject) => {
      const request = http.request({ socketPath: socket, method, path: target, headers }, (response) => {
        const chunks: Buffer[] = [];
        response.on('data', (chunk: Buffer) => chunks.push(chunk));
        response.on('end', () =>
          resolve({ status: response.statusCode!, headers: response.headers, body: Buffer.concat(chunks).toString() }),
        );
      });
      request.on('error', reject);
      if (body !== undefined) request.write(body);
      request.end();
    });
  }

  it('serves the contract over the socket with no-store and a private socket file', async () => {
    expect(fs.statSync(socket).mode & 0o777).toBe(0o660);
    const response = await call('GET', '/api/v1/agents');
    expect(response.status).toBe(200);
    expect(response.headers['cache-control']).toBe('no-store');
    expect(response.headers['x-content-type-options']).toBe('nosniff');
    expect(findLeaks(response.body)).toEqual([]);
  });

  it('refuses oversized, non-JSON and malformed bodies', async () => {
    const target = endpoint('restart_agent').path.replace('{agent}', mainId);
    expect(
      (await call('POST', target, 'x'.repeat(MAX_BODY_BYTES + 1), { 'content-type': 'application/json' })).status,
    ).toBe(413);
    expect((await call('POST', target, '{"confirm":true}', { 'content-type': 'text/plain' })).status).toBe(415);
    expect((await call('POST', target, '{', { 'content-type': 'application/json' })).status).toBe(400);
    expect((await call('PUT', '/api/v1/agents')).status).toBe(405);
  });

  it('refuses a socket directory open to others', async () => {
    await stopDashboardAdminSocket();
    fs.chmodSync(directory, 0o757);
    await expect(startDashboardAdminSocket(socket, sources())).rejects.toThrow('closed to others');
  });
});

describe('public id key', () => {
  it('is created once, private, and stable', () => {
    const directory = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-key-')), 'dashboard');
    const first = loadIdKey(directory);
    expect(first.length).toBe(32);
    expect(fs.statSync(path.join(directory, 'id-key')).mode & 0o777).toBe(0o600);
    expect(loadIdKey(directory).equals(first)).toBe(true);
    fs.chmodSync(path.join(directory, 'id-key'), 0o644);
    expect(() => loadIdKey(directory)).toThrow('private file');
    fs.rmSync(path.dirname(directory), { recursive: true, force: true });
  });
});
