/**
 * Install-wide provider/model change from the dashboard (D3a).
 *
 * One choice for every agent: the preflight checks the endpoint and model
 * (src/dashboard/host/model-endpoint.ts) and every agent group, and the apply
 * job moves the default for new groups and every existing group together.
 * v1 supports the local path only: OpenCode against an OpenAI-compatible
 * endpoint on the LAN. External providers are refused at preflight until
 * their credentials can be checked per agent.
 *
 * The job writes a journal before it changes anything: the previous settings
 * file and every group's provider and model (and any session pinned to
 * another provider). On failure it restores that snapshot, restarts the agents
 * again and checks the rows; if the host dies mid-change, the next start
 * restores it (`recoverModelApply`). The operations service refuses backups
 * and updates while the journal exists. No step logs the endpoint or model.
 */
import { randomBytes } from 'crypto';
import fs from 'fs';
import path from 'path';

import { DATA_DIR } from '../../config.js';
import { getDb } from '../../db/connection.js';
import { ensureContainerConfig } from '../../db/container-configs.js';
import { log } from '../../log.js';
import {
  readModelSettings,
  readModelSettingsRaw,
  writeModelSettingsRaw,
  writePrivateFileAtomic,
  type ModelSettings,
} from '../../model-settings.js';
import { getProviderContainerConfig } from '../../providers/provider-container-registry.js';
import { modelName, type job as jobSchema, type modelPreflight } from '../contract/api.js';
import { publicId } from '../contract/opaque-id.js';
import { validate, type Infer } from '../contract/schema.js';
import type { HostSources } from './projections.js';

export const JOURNAL_FILE = 'model-settings.journal.json';
export const JOB_FILE = 'model-settings.job.json';
const PREFLIGHT_TTL_MS = 10 * 60_000;
const MAX_AGENTS = 200;
const PROVIDER = 'opencode';
const BACKEND = 'openai';

type Job = Infer<typeof jobSchema>;
type Preflight = Infer<typeof modelPreflight>;

export class ModelApplyError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}

interface Journal {
  version: 1;
  job: string;
  previous_settings: string | null;
  groups: Array<{ id: string; had_row: boolean; provider: string | null; model: string | null }>;
  sessions: Array<{ id: string; agent_provider: string | null }>;
}

interface Pending {
  endpoint: string;
  model: string;
  contextLimit: number | null;
  groups: string;
  expires: number;
  ready: boolean;
}

const preflights = new Map<string, Pending>();
let running: Promise<void> | null = null;

const seconds = (date: Date) => date.toISOString().replace(/\.\d{3}Z$/, 'Z');

async function groupIds(): Promise<string[]> {
  const rows = await getDb().all<{ id: string }>('SELECT id FROM agent_groups ORDER BY id');
  return rows.map((row) => row.id);
}

function prune(now: number): void {
  for (const [id, entry] of preflights) if (entry.expires <= now) preflights.delete(id);
  while (preflights.size > 20) preflights.delete(preflights.keys().next().value!);
}

export async function preflightModelChange(
  sources: HostSources,
  body: { mode: string; provider: string; model: string; endpoint: string | null },
  restarting: ReadonlySet<string>,
): Promise<Preflight> {
  const now = sources.now();
  const ids = await groupIds();
  let reason: string | null = null;
  if (body.mode !== 'local' || body.provider !== PROVIDER) reason = 'provider_unsupported';
  else if (!getProviderContainerConfig(PROVIDER)) reason = 'provider_not_installed';
  else if (!body.endpoint) reason = 'endpoint_required';
  else if (validate(modelName, `${BACKEND}/${body.model}`).length) reason = 'model_invalid';
  else if (ids.length > MAX_AGENTS) reason = 'too_many_agents';

  let models: string[] = [];
  let contextLimit: number | null = null;
  if (!reason) {
    const probe = await sources.probeModel(body.endpoint!, body.model);
    reason = probe.reason;
    models = probe.models;
    contextLimit = probe.contextLimit;
  }

  const sessions = await getDb().all<{ agent_group_id: string; status: string; container_status: string }>(
    'SELECT agent_group_id, status, container_status FROM sessions',
  );
  const agents = ids.slice(0, MAX_AGENTS).map((id) => {
    const own = reason ?? (restarting.has(id) ? 'restart_in_progress' : null);
    return { id: publicId('agent', id, sources.idKey), ready: own === null, reason: own };
  });
  const ready = reason === null && agents.every((agent) => agent.ready);

  prune(now.getTime());
  const id = `pfl_${randomBytes(8).toString('hex')}`;
  const expires = now.getTime() + PREFLIGHT_TTL_MS;
  preflights.set(id, {
    endpoint: body.endpoint ?? '',
    model: `${BACKEND}/${body.model}`,
    contextLimit,
    groups: ids.join('\n'),
    expires,
    ready,
  });
  return {
    preflight_id: id,
    ready,
    reason,
    leaves_lan: false,
    agents,
    sessions_to_restart: sessions.filter(
      (session) =>
        session.status === 'active' && (session.container_status === 'running' || session.container_status === 'idle'),
    ).length,
    available_models: models.filter((item) => validate(modelName, item).length === 0).slice(0, 50),
    expires_at: seconds(new Date(expires)),
  };
}

// ── Job record ──

function jobPath(dataDir: string): string {
  return path.join(dataDir, JOB_FILE);
}

function saveJob(dataDir: string, record: Job): void {
  writePrivateFileAtomic(jobPath(dataDir), `${JSON.stringify(record)}\n`);
}

export function readJob(dataDir: string, id?: string): Job | null {
  try {
    const record = JSON.parse(fs.readFileSync(jobPath(dataDir), 'utf-8')) as Job;
    return id === undefined || record.id === id ? record : null;
  } catch {
    return null;
  }
}

function advance(dataDir: string, record: Job, phase: string, at: Date): void {
  record.phase = phase;
  record.updated_at = seconds(at);
  if (record.phases.length < 32) record.phases.push({ phase, at: record.updated_at });
  saveJob(dataDir, record);
}

function finish(
  dataDir: string,
  record: Job,
  at: Date,
  outcome: Job['outcome'],
  rollback: Job['rollback'],
  failure: string | null,
): void {
  record.outcome = outcome;
  record.rollback = rollback;
  record.failure_category = failure;
  record.updated_at = seconds(at);
  record.finished_at = record.updated_at;
  saveJob(dataDir, record);
}

// ── Journal ──

function journalPath(dataDir: string): string {
  return path.join(dataDir, JOURNAL_FILE);
}

function readJournal(dataDir: string): Journal | null {
  let raw: string;
  try {
    raw = fs.readFileSync(journalPath(dataDir), 'utf-8');
  } catch {
    return null;
  }
  const value = JSON.parse(raw) as Journal;
  if (value?.version !== 1 || !Array.isArray(value.groups) || !Array.isArray(value.sessions)) {
    throw new Error('model settings journal has an unexpected shape');
  }
  return value;
}

async function snapshot(dataDir: string, jobId: string): Promise<Journal> {
  const db = getDb();
  const rows = new Map(
    (
      await db.all<{ agent_group_id: string; provider: string | null; model: string | null }>(
        'SELECT agent_group_id, provider, model FROM container_configs',
      )
    ).map((row) => [row.agent_group_id, row]),
  );
  const journal: Journal = {
    version: 1,
    job: jobId,
    previous_settings: readModelSettingsRaw(dataDir),
    groups: (await groupIds()).map((id) => {
      const row = rows.get(id);
      return { id, had_row: Boolean(row), provider: row?.provider ?? null, model: row?.model ?? null };
    }),
    sessions: await db.all<{ id: string; agent_provider: string | null }>(
      `SELECT id, agent_provider FROM sessions WHERE agent_provider IS NOT NULL AND lower(agent_provider) <> ?`,
      PROVIDER,
    ),
  };
  writePrivateFileAtomic(journalPath(dataDir), `${JSON.stringify(journal)}\n`);
  return journal;
}

/** Put back every group, pinned session and the settings file exactly as the journal recorded them. */
async function restore(dataDir: string, journal: Journal): Promise<void> {
  const db = getDb();
  const at = new Date().toISOString();
  await db.transaction(async () => {
    for (const group of journal.groups) {
      if (group.had_row) {
        await db.run(
          'UPDATE container_configs SET provider = ?, model = ?, updated_at = ? WHERE agent_group_id = ?',
          group.provider,
          group.model,
          at,
          group.id,
        );
      } else {
        await db.run('DELETE FROM container_configs WHERE agent_group_id = ?', group.id);
      }
    }
    for (const session of journal.sessions) {
      await db.run('UPDATE sessions SET agent_provider = ? WHERE id = ?', session.agent_provider, session.id);
    }
  });
  writeModelSettingsRaw(journal.previous_settings, dataDir);
}

async function rowsMatch(expected: (id: string) => { provider: string | null; model: string | null } | null) {
  const rows = await getDb().all<{ agent_group_id: string; provider: string | null; model: string | null }>(
    'SELECT agent_group_id, provider, model FROM container_configs',
  );
  const byId = new Map(rows.map((row) => [row.agent_group_id, row]));
  for (const id of await groupIds()) {
    const want = expected(id);
    const row = byId.get(id);
    if (want === null ? row !== undefined : row?.provider !== want.provider || row?.model !== want.model) return false;
  }
  return true;
}

// ── Apply ──

export async function applyModelChange(
  sources: HostSources,
  preflightId: string,
): Promise<{ job: { id: string; kind: 'model_settings_apply' } }> {
  const now = sources.now();
  const pending = preflights.get(preflightId);
  if (!pending || pending.expires <= now.getTime()) throw new ModelApplyError(409, 'preflight_expired');
  if (!pending.ready) throw new ModelApplyError(409, 'preflight_not_ready');
  if (running || fs.existsSync(journalPath(sources.dataDir))) {
    throw new ModelApplyError(409, 'operation_in_progress');
  }
  if ((await groupIds()).join('\n') !== pending.groups) throw new ModelApplyError(409, 'preflight_stale');
  preflights.delete(preflightId);

  const at = seconds(now);
  const record: Job = {
    id: `job_${randomBytes(8).toString('hex')}`,
    kind: 'model_settings_apply',
    phase: 'snapshot',
    outcome: 'running',
    failure_category: null,
    rollback: null,
    release: null,
    backup: null,
    phases: [{ phase: 'snapshot', at }],
    started_at: at,
    updated_at: at,
    finished_at: null,
  };
  saveJob(sources.dataDir, record);
  running = run(sources, record, pending).finally(() => {
    running = null;
  });
  return { job: { id: record.id, kind: 'model_settings_apply' } };
}

/** Resolves when the job in flight (if any) has finished; for tests and shutdown. */
export function modelApplySettled(): Promise<void> {
  return running ?? Promise.resolve();
}

async function restartAll(sources: HostSources, ids: string[]): Promise<void> {
  for (const id of ids) await sources.restartAgent(id);
}

async function run(sources: HostSources, record: Job, pending: Pending): Promise<void> {
  const { dataDir } = sources;
  const settings: ModelSettings = {
    version: 1,
    provider: PROVIDER,
    backend: BACKEND,
    endpoint: pending.endpoint,
    model: pending.model,
    context_limit: pending.contextLimit,
    applied_at: new Date().toISOString(),
  };
  let journal: Journal;
  try {
    journal = await snapshot(dataDir, record.id);
  } catch {
    log.error('Model settings change failed before any change', { job: record.id, phase: 'snapshot' });
    finish(dataDir, record, sources.now(), 'failed', 'not_needed', 'snapshot_failed');
    return;
  }
  const ids = journal.groups.map((group) => group.id);
  let failure = 'apply_failed';
  try {
    advance(dataDir, record, 'write_settings', sources.now());
    writeModelSettingsRaw(`${JSON.stringify(settings)}\n`, dataDir);

    advance(dataDir, record, 'update_agents', sources.now());
    const db = getDb();
    const at = new Date().toISOString();
    await db.transaction(async () => {
      for (const id of ids) {
        await ensureContainerConfig(id, PROVIDER);
        await db.run(
          'UPDATE container_configs SET provider = ?, model = ?, updated_at = ? WHERE agent_group_id = ?',
          PROVIDER,
          settings.model,
          at,
          id,
        );
      }
      for (const session of journal.sessions) {
        await db.run('UPDATE sessions SET agent_provider = ? WHERE id = ?', PROVIDER, session.id);
      }
    });

    advance(dataDir, record, 'restart_agents', sources.now());
    failure = 'restart_failed';
    await restartAll(sources, ids);

    advance(dataDir, record, 'verify', sources.now());
    failure = 'verify_failed';
    const stored = readModelSettings(dataDir);
    if (stored?.endpoint !== settings.endpoint || stored.model !== settings.model) throw new Error('settings');
    if (!(await rowsMatch(() => ({ provider: PROVIDER, model: settings.model })))) throw new Error('rows');
    const probe = await sources.probeModel(settings.endpoint, settings.model.slice(BACKEND.length + 1));
    if (probe.reason) {
      failure = probe.reason;
      throw new Error('probe');
    }

    fs.rmSync(journalPath(dataDir), { force: true });
    advance(dataDir, record, 'done', sources.now());
    finish(dataDir, record, sources.now(), 'succeeded', 'not_needed', null);
    log.info('Model settings changed from the dashboard', { job: record.id, agents: ids.length });
  } catch {
    log.error('Model settings change failed; restoring the previous settings', { job: record.id, failure });
    advance(dataDir, record, 'rollback', sources.now());
    let restored = false;
    try {
      await restore(dataDir, journal);
      await restartAll(sources, ids);
      const previous = new Map(journal.groups.map((group) => [group.id, group]));
      restored =
        (await rowsMatch((id) => {
          const group = previous.get(id);
          return group?.had_row ? { provider: group.provider, model: group.model } : null;
        })) && readModelSettingsRaw(dataDir) === journal.previous_settings;
    } catch {
      restored = false;
    }
    if (restored) {
      fs.rmSync(journalPath(dataDir), { force: true });
      finish(dataDir, record, sources.now(), 'rolled_back', 'healthy', failure);
    } else {
      // The journal stays: the next host start retries, and backups/updates wait.
      log.error('Model settings rollback failed; manual recovery needed', { job: record.id });
      finish(dataDir, record, sources.now(), 'rollback_failed', 'failed_manual_recovery_needed', failure);
    }
  }
}

/**
 * At host start: a journal left behind means a change was cut off (host
 * stopped, crash, update). Restore the previous settings before any agent
 * starts. Failure is logged and the journal kept for manual recovery.
 */
export async function recoverModelApply(dataDir = DATA_DIR): Promise<void> {
  let journal: Journal | null;
  try {
    journal = readJournal(dataDir);
  } catch {
    log.error('Model settings journal unreadable; manual recovery needed');
    return;
  }
  if (!journal) {
    // A job still marked running with no journal either stopped before the
    // snapshot (nothing changed) or after verification removed the journal.
    const last = readJob(dataDir);
    if (last?.outcome === 'running') {
      if (last.phase === 'verify' || last.phase === 'done') {
        finish(dataDir, last, new Date(), 'succeeded', 'not_needed', null);
      } else finish(dataDir, last, new Date(), 'interrupted', 'not_needed', 'interrupted');
    }
    return;
  }
  const record = readJob(dataDir, journal.job);
  try {
    await restore(dataDir, journal);
    fs.rmSync(journalPath(dataDir), { force: true });
    if (record && record.outcome === 'running') {
      advance(dataDir, record, 'rollback', new Date());
      finish(dataDir, record, new Date(), 'interrupted', 'healthy', 'interrupted');
    }
    log.warn('Interrupted model settings change rolled back at startup', { job: journal.job });
  } catch {
    log.error('Model settings rollback at startup failed; manual recovery needed', { job: journal.job });
    if (record && record.outcome === 'running') {
      finish(dataDir, record, new Date(), 'rollback_failed', 'failed_manual_recovery_needed', 'interrupted');
    }
  }
}

/** Test hook: forget preflights and any job handle. */
export function resetModelApplyForTests(): void {
  preflights.clear();
  running = null;
}
