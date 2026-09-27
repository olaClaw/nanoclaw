/**
 * Install-wide provider/model chosen from the dashboard (D3a).
 *
 * The panel never edits `.env`. Its choice lives in `data/model-settings.json`
 * (0600, host-owned, inside every backup) and wins over the `.env` values it
 * replaces: the default provider and model for new groups, and the endpoint
 * and model every OpenCode container starts with. No file means `.env` alone,
 * exactly as before. Everything else in `.env` (limits, modalities, auth mode)
 * stays where it is.
 *
 * Applying a change is a journaled job (src/dashboard/host/model-apply.ts);
 * the journal and the job record live next to this file.
 */
import fs from 'fs';
import path from 'path';

import { DATA_DIR, DEFAULT_AGENT_PROVIDER, DEFAULT_MODEL } from './config.js';
import { log } from './log.js';

export const MODEL_SETTINGS_FILE = 'model-settings.json';

export interface ModelSettings {
  version: 1;
  /** Agent provider for every group; v1 supports the local OpenCode path only. */
  provider: 'opencode';
  /** OpenCode's backend: `openai` with a base URL is an OpenAI-compatible server. */
  backend: 'openai';
  /** OpenAI-compatible base URL on the LAN, e.g. `http://LLM_HOST:8000/v1`. */
  endpoint: string;
  /** `backend/model-id`, the form group configs and OpenCode prompts use. */
  model: string;
  /** Context window the endpoint reports for the model, when it reports one. */
  context_limit: number | null;
  applied_at: string;
}

let cache: { key: string; value: ModelSettings | null } | null = null;

function parse(raw: string): ModelSettings | null {
  const value = JSON.parse(raw) as Record<string, unknown>;
  const ok =
    value &&
    typeof value === 'object' &&
    value.version === 1 &&
    value.provider === 'opencode' &&
    value.backend === 'openai' &&
    typeof value.endpoint === 'string' &&
    /^https?:\/\/\S+$/.test(value.endpoint) &&
    typeof value.model === 'string' &&
    value.model.startsWith('openai/') &&
    value.model.length > 'openai/'.length &&
    value.model.length <= 256 &&
    (value.context_limit === null ||
      (typeof value.context_limit === 'number' &&
        Number.isSafeInteger(value.context_limit) &&
        value.context_limit > 0)) &&
    typeof value.applied_at === 'string';
  return ok ? (value as unknown as ModelSettings) : null;
}

/** The dashboard's choice, or null when the panel never set one (or the file is unusable). */
export function readModelSettings(dataDir = DATA_DIR): ModelSettings | null {
  const file = path.join(dataDir, MODEL_SETTINGS_FILE);
  let stat: fs.Stats;
  try {
    stat = fs.lstatSync(file);
  } catch {
    cache = null;
    return null;
  }
  const key = `${file}:${stat.ino}:${stat.size}:${stat.mtimeMs}`;
  if (cache?.key === key) return cache.value;
  let value: ModelSettings | null = null;
  try {
    if (!stat.isFile()) throw new Error('not a regular file');
    value = parse(fs.readFileSync(file, 'utf-8'));
    if (!value) throw new Error('unexpected shape');
  } catch (err) {
    // Category only: the file holds a LAN address.
    log.error('Dashboard model settings unreadable; using .env defaults', {
      reason: err instanceof Error ? err.message.slice(0, 40) : 'unknown',
    });
    value = null;
  }
  cache = { key, value };
  return value;
}

/** Raw file content, for the apply journal's snapshot. */
export function readModelSettingsRaw(dataDir = DATA_DIR): string | null {
  try {
    return fs.readFileSync(path.join(dataDir, MODEL_SETTINGS_FILE), 'utf-8');
  } catch {
    return null;
  }
}

/** Write a small private file atomically: temp file, fsync, rename, fsync the directory. */
export function writePrivateFileAtomic(file: string, content: string): void {
  const temporary = `${file}.${process.pid}.${Date.now()}.tmp`;
  const fd = fs.openSync(temporary, 'wx', 0o600);
  try {
    fs.writeFileSync(fd, content);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  try {
    fs.renameSync(temporary, file);
  } catch (err) {
    fs.rmSync(temporary, { force: true });
    throw err;
  }
  const dir = fs.openSync(path.dirname(file), 'r');
  try {
    fs.fsyncSync(dir);
  } finally {
    fs.closeSync(dir);
  }
}

/** Replace the settings file with `raw` (a snapshot or a new value); null removes it. */
export function writeModelSettingsRaw(raw: string | null, dataDir = DATA_DIR): void {
  const file = path.join(dataDir, MODEL_SETTINGS_FILE);
  cache = null;
  if (raw === null) fs.rmSync(file, { force: true });
  else writePrivateFileAtomic(file, raw);
}

/** Provider stamped onto new groups: the panel's choice, else `DEFAULT_AGENT_PROVIDER`. */
export function defaultAgentProvider(): string {
  return readModelSettings()?.provider ?? DEFAULT_AGENT_PROVIDER;
}

/** Model for groups without their own: the panel's choice, else `NANOCLAW_DEFAULT_MODEL`. */
export function defaultModel(): string {
  return readModelSettings()?.model ?? DEFAULT_MODEL;
}

/**
 * The OpenCode variables the panel's choice replaces. The small model follows
 * the main one (an old small model may not exist on the new endpoint), and a
 * context window the endpoint reported replaces the `.env` limit, dropping an
 * output limit that would no longer fit inside it.
 */
export function applyOpenCodeOverrides(env: Record<string, string>, dataDir = DATA_DIR): void {
  const settings = readModelSettings(dataDir);
  if (!settings) return;
  env.OPENCODE_PROVIDER = settings.backend;
  env.OPENCODE_BASE_URL = settings.endpoint;
  env.OPENCODE_MODEL = settings.model;
  env.OPENCODE_SMALL_MODEL = settings.model;
  if (settings.context_limit !== null) {
    env.OPENCODE_MODEL_CONTEXT_LIMIT = String(settings.context_limit);
    const output = Number(env.OPENCODE_MODEL_OUTPUT_LIMIT);
    if (env.OPENCODE_MODEL_OUTPUT_LIMIT !== undefined && !(output > 0 && output < settings.context_limit)) {
      delete env.OPENCODE_MODEL_OUTPUT_LIMIT;
    }
  }
}
