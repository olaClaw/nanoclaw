import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import '../../providers/opencode.js';
import { closeDb, getDb, initTestDb } from '../../db/connection.js';
import { createAgentGroup } from '../../db/agent-groups.js';
import { runMigrations } from '../../db/migrations/index.js';
import { MODEL_SETTINGS_FILE, readModelSettings } from '../../model-settings.js';
import { endpoint } from '../contract/api.js';
import { publicId } from '../contract/opaque-id.js';
import { validate } from '../contract/schema.js';
import { CANARIES, findLeaks } from '../fixtures/canaries.js';
import { SYNTHETIC, seedSyntheticInstall } from '../fixtures/synthetic-install.js';
import { handleAdminRequest } from './boundary.js';
import {
  JOB_FILE,
  JOURNAL_FILE,
  modelApplySettled,
  recoverModelApply,
  resetModelApplyForTests,
} from './model-apply.js';
import type { ProbeResult } from './model-endpoint.js';
import type { HostSources } from './projections.js';

const KEY = Buffer.alloc(32, 7);
const ENDPOINT = 'http://100.64.1.2:8000/v1';
const LOOSE = 'ag-fixture-no-config';
const PINNED = SYNTHETIC.sessions.find((session) => session.agent === 'helper')!.id;

let dataDir: string;
let clock: number;
let probe: (endpoint: string, model: string | null) => Promise<ProbeResult>;
let restarts: string[];

function sources(changes: Partial<HostSources> = {}): HostSources {
  return {
    idKey: KEY,
    channels: () => [],
    release: () => ({ version: '2.4.0', revision: 'a'.repeat(40) }),
    defaults: { provider: 'opencode', model: '', opencodeModel: '', endpointConfigured: true },
    now: () => new Date(clock),
    restartAgent: vi.fn(async (id: string) => {
      restarts.push(id);
      return 1;
    }),
    dataDir,
    probeModel: (endpoint, model) => probe(endpoint, model),
    endpointState: async () => 'reachable',
    ...changes,
  };
}

async function call(method: string, target: string, body: unknown, host = sources()) {
  const response = await handleAdminRequest({ method, target, body }, host);
  if (response.endpoint) {
    const schema = response.status < 300 ? endpoint(response.endpoint).response : null;
    if (schema) expect(validate(schema, response.body)).toEqual([]);
  }
  expect(findLeaks(JSON.stringify(response.body))).toEqual([]);
  expect(JSON.stringify(response.body)).not.toContain('100.64.1.2');
  return response;
}

const preflight = (body: Record<string, unknown> = {}, host?: HostSources) =>
  call(
    'POST',
    '/api/v1/model-settings/preflight',
    { mode: 'local', provider: 'opencode', model: 'fixture-model-b', endpoint: ENDPOINT, ...body },
    host,
  );

async function rows() {
  return getDb().all<{ agent_group_id: string; provider: string | null; model: string | null }>(
    'SELECT agent_group_id, provider, model FROM container_configs ORDER BY agent_group_id',
  );
}

async function pins() {
  return getDb().all<{ id: string; agent_provider: string | null }>(
    'SELECT id, agent_provider FROM sessions WHERE agent_provider IS NOT NULL ORDER BY id',
  );
}

async function job(id: string, host?: HostSources) {
  await modelApplySettled();
  const response = await call('GET', `/api/v1/jobs/${id}`, undefined, host);
  expect(response.status).toBe(200);
  return response.body as {
    outcome: string;
    rollback: string | null;
    failure_category: string | null;
    phases: Array<{ phase: string }>;
  };
}

beforeEach(async () => {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-model-apply-'));
  clock = Date.parse('2026-01-15T12:00:00Z');
  restarts = [];
  probe = async () => ({
    reason: null,
    models: ['fixture-model-a', 'fixture-model-b', 'not a model!'],
    contextLimit: 32768,
  });
  resetModelApplyForTests();
  await runMigrations(await initTestDb());
  await seedSyntheticInstall();
  // A group that never started has no config row yet; one session is pinned to Claude.
  await createAgentGroup({
    id: LOOSE,
    name: 'Loose',
    folder: 'fixture-loose',
    agent_provider: null,
    created_at: '2026-01-15T09:00:00.000Z',
  });
  await getDb().run("UPDATE sessions SET agent_provider = 'claude' WHERE id = ?", PINNED);
});

afterEach(async () => {
  await modelApplySettled();
  await closeDb();
  fs.rmSync(dataDir, { recursive: true, force: true });
});

describe('model change preflight', () => {
  it('lists every agent, the sessions to restart and the models to choose from', async () => {
    const response = await preflight();
    expect(response.status).toBe(200);
    const body = response.body as {
      ready: boolean;
      reason: string | null;
      leaves_lan: boolean;
      agents: Array<{ id: string; ready: boolean }>;
      sessions_to_restart: number;
      available_models: string[];
    };
    expect(body).toMatchObject({ ready: true, reason: null, leaves_lan: false, sessions_to_restart: 2 });
    expect(body.agents.map((agent) => agent.id).sort()).toEqual(
      [SYNTHETIC.agents.main.id, SYNTHETIC.agents.helper.id, LOOSE].map((id) => publicId('agent', id, KEY)).sort(),
    );
    expect(body.available_models).toEqual(['fixture-model-a', 'fixture-model-b']);
  });

  it('refuses external providers, a missing endpoint and a failed probe for every agent', async () => {
    for (const [body, reason] of [
      [{ mode: 'external', provider: 'claude', endpoint: null }, 'provider_unsupported'],
      [{ provider: 'claude' }, 'provider_unsupported'],
      [{ endpoint: null }, 'endpoint_required'],
    ] as const) {
      const response = (await preflight(body)).body as {
        ready: boolean;
        reason: string;
        agents: Array<{ reason: string }>;
      };
      expect([response.ready, response.reason], reason).toEqual([false, reason]);
      expect(new Set(response.agents.map((agent) => agent.reason))).toEqual(new Set([reason]));
    }
    probe = async () => ({ reason: 'model_not_found', models: ['fixture-model-a'], contextLimit: null });
    const missing = (await preflight()).body as { ready: boolean; reason: string; available_models: string[] };
    expect(missing).toMatchObject({ ready: false, reason: 'model_not_found', available_models: ['fixture-model-a'] });
  });
});

describe('model change apply', () => {
  it('moves the default and every group together, restarts all agents and records the job', async () => {
    const id = ((await preflight()).body as { preflight_id: string }).preflight_id;
    const accepted = await call('POST', '/api/v1/model-settings/apply', { preflight_id: id, confirm: true });
    expect(accepted.status).toBe(200);
    const jobId = (accepted.body as { job: { id: string; kind: string } }).job.id;

    const record = await job(jobId);
    expect(record).toMatchObject({ outcome: 'succeeded', rollback: 'not_needed', failure_category: null });
    expect(record.phases.map((phase) => phase.phase)).toEqual([
      'snapshot',
      'write_settings',
      'update_agents',
      'restart_agents',
      'verify',
      'done',
    ]);
    expect(await rows()).toEqual(
      [LOOSE, SYNTHETIC.agents.helper.id, SYNTHETIC.agents.main.id]
        .sort()
        .map((agent_group_id) => ({ agent_group_id, provider: 'opencode', model: 'openai/fixture-model-b' })),
    );
    expect(await pins()).toEqual([{ id: PINNED, agent_provider: 'opencode' }]);
    expect(restarts.sort()).toEqual([LOOSE, SYNTHETIC.agents.helper.id, SYNTHETIC.agents.main.id].sort());

    const settings = readModelSettings(dataDir)!;
    expect(settings).toMatchObject({
      provider: 'opencode',
      backend: 'openai',
      endpoint: ENDPOINT,
      model: 'openai/fixture-model-b',
      context_limit: 32768,
    });
    expect(fs.statSync(path.join(dataDir, MODEL_SETTINGS_FILE)).mode & 0o777).toBe(0o600);
    expect(fs.existsSync(path.join(dataDir, JOURNAL_FILE))).toBe(false);

    // The preflight is single-use.
    const again = await call('POST', '/api/v1/model-settings/apply', { preflight_id: id, confirm: true });
    expect([again.status, (again.body as { error: { code: string } }).error.code]).toEqual([409, 'preflight_expired']);
  });

  it('refuses a preflight that failed, expired, or no longer matches the agents', async () => {
    const apply = async (preflightId: string) => {
      const response = await call('POST', '/api/v1/model-settings/apply', { preflight_id: preflightId, confirm: true });
      return [response.status, (response.body as { error: { code: string } }).error.code];
    };
    probe = async () => ({ reason: 'inference_failed', models: [], contextLimit: null });
    expect(await apply(((await preflight()).body as { preflight_id: string }).preflight_id)).toEqual([
      409,
      'preflight_not_ready',
    ]);

    probe = async () => ({ reason: null, models: ['fixture-model-b'], contextLimit: null });
    const stale = ((await preflight()).body as { preflight_id: string }).preflight_id;
    await createAgentGroup({
      id: 'ag-fixture-late',
      name: 'Late',
      folder: 'fixture-late',
      agent_provider: null,
      created_at: '2026-01-15T11:00:00.000Z',
    });
    expect(await apply(stale)).toEqual([409, 'preflight_stale']);

    const late = ((await preflight()).body as { preflight_id: string }).preflight_id;
    clock += 11 * 60_000;
    expect(await apply(late)).toEqual([409, 'preflight_expired']);
    expect(await apply('pfl_0000000000000000')).toEqual([409, 'preflight_expired']);
    expect(restarts).toEqual([]);
  });

  it('restores every group, pin and the previous settings file when a step fails', async () => {
    const previous = `${JSON.stringify({
      version: 1,
      provider: 'opencode',
      backend: 'openai',
      endpoint: 'http://100.64.9.9:8000/v1',
      model: 'openai/fixture-model-a',
      context_limit: null,
      applied_at: '2026-01-01T00:00:00.000Z',
    })}\n`;
    fs.writeFileSync(path.join(dataDir, MODEL_SETTINGS_FILE), previous, { mode: 0o600 });
    const before = await rows();

    let calls = 0;
    const failing = sources({
      restartAgent: async (id) => {
        restarts.push(id);
        calls += 1;
        if (calls === 2) throw new Error('docker unavailable');
        return 1;
      },
    });
    const id = ((await preflight({}, failing)).body as { preflight_id: string }).preflight_id;
    const accepted = await call('POST', '/api/v1/model-settings/apply', { preflight_id: id, confirm: true }, failing);
    const record = await job((accepted.body as { job: { id: string } }).job.id, failing);

    expect(record).toMatchObject({ outcome: 'rolled_back', rollback: 'healthy', failure_category: 'restart_failed' });
    expect(record.phases.at(-1)!.phase).toBe('rollback');
    expect(await rows()).toEqual(before);
    expect(await pins()).toEqual([{ id: PINNED, agent_provider: 'claude' }]);
    expect(fs.readFileSync(path.join(dataDir, MODEL_SETTINGS_FILE), 'utf-8')).toBe(previous);
    expect(fs.existsSync(path.join(dataDir, JOURNAL_FILE))).toBe(false);
  });

  it('keeps the journal and asks for manual recovery when the rollback cannot finish', async () => {
    const broken = sources({
      restartAgent: async () => {
        throw new Error('docker unavailable');
      },
    });
    const id = ((await preflight({}, broken)).body as { preflight_id: string }).preflight_id;
    const accepted = await call('POST', '/api/v1/model-settings/apply', { preflight_id: id, confirm: true }, broken);
    const record = await job((accepted.body as { job: { id: string } }).job.id, broken);
    expect(record).toMatchObject({ outcome: 'rollback_failed', rollback: 'failed_manual_recovery_needed' });
    expect(fs.existsSync(path.join(dataDir, JOURNAL_FILE))).toBe(true);
    // The next start finishes the restore.
    await recoverModelApply(dataDir);
    expect(fs.existsSync(path.join(dataDir, JOURNAL_FILE))).toBe(false);
    expect(fs.existsSync(path.join(dataDir, MODEL_SETTINGS_FILE))).toBe(false);
  });

  it('rolls back when the endpoint stops answering after the switch', async () => {
    const id = ((await preflight()).body as { preflight_id: string }).preflight_id;
    probe = async () => ({ reason: 'endpoint_unreachable', models: [], contextLimit: null });
    const accepted = await call('POST', '/api/v1/model-settings/apply', { preflight_id: id, confirm: true });
    const record = await job((accepted.body as { job: { id: string } }).job.id);
    expect(record).toMatchObject({
      outcome: 'rolled_back',
      rollback: 'healthy',
      failure_category: 'endpoint_unreachable',
    });
    expect(fs.existsSync(path.join(dataDir, MODEL_SETTINGS_FILE))).toBe(false);
    expect((await rows()).find((row) => row.agent_group_id === LOOSE)).toBeUndefined();
  });
});

describe('startup recovery', () => {
  it('rolls back a change the host did not finish and marks its job interrupted', async () => {
    const before = await rows();
    const id = ((await preflight()).body as { preflight_id: string }).preflight_id;
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let entered = false;
    const held = sources({
      restartAgent: async () => {
        entered = true;
        await gate;
        return 1;
      },
    });
    const accepted = await call('POST', '/api/v1/model-settings/apply', { preflight_id: id, confirm: true }, held);
    const jobId = (accepted.body as { job: { id: string } }).job.id;
    await vi.waitFor(() => expect(entered).toBe(true));
    // The host "dies" here: the journal and a running job record stay behind.
    expect(fs.existsSync(path.join(dataDir, JOURNAL_FILE))).toBe(true);
    const journal = fs.readFileSync(path.join(dataDir, JOURNAL_FILE), 'utf-8');
    const record = fs.readFileSync(path.join(dataDir, JOB_FILE), 'utf-8');
    release!();
    await modelApplySettled();
    fs.writeFileSync(path.join(dataDir, JOURNAL_FILE), journal, { mode: 0o600 });
    fs.writeFileSync(path.join(dataDir, JOB_FILE), record, { mode: 0o600 });

    // Refused while the journal exists.
    const blocked = await call('POST', '/api/v1/model-settings/apply', {
      preflight_id: ((await preflight()).body as { preflight_id: string }).preflight_id,
      confirm: true,
    });
    expect((blocked.body as { error: { code: string } }).error.code).toBe('operation_in_progress');

    await recoverModelApply(dataDir);
    expect(await rows()).toEqual(before);
    expect(await pins()).toEqual([{ id: PINNED, agent_provider: 'claude' }]);
    expect(fs.existsSync(path.join(dataDir, MODEL_SETTINGS_FILE))).toBe(false);
    expect(fs.existsSync(path.join(dataDir, JOURNAL_FILE))).toBe(false);
    expect(await job(jobId)).toMatchObject({ outcome: 'interrupted', rollback: 'healthy' });
  });

  it('never returns the endpoint or other install values in the job record', async () => {
    const id = ((await preflight()).body as { preflight_id: string }).preflight_id;
    const accepted = await call('POST', '/api/v1/model-settings/apply', { preflight_id: id, confirm: true });
    await job((accepted.body as { job: { id: string } }).job.id);
    const onDisk = fs.readFileSync(path.join(dataDir, JOB_FILE), 'utf-8');
    expect(onDisk).not.toContain('100.64.1.2');
    expect(onDisk).not.toContain(CANARIES.folder);
  });
});
