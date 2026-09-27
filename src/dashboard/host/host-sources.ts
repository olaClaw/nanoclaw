/**
 * Live host state for the dashboard projections, and the per-install key for
 * public IDs. Kept apart from the projections so tests can inject both.
 */
import { randomBytes } from 'crypto';
import fs from 'fs';
import path from 'path';

import { getActiveAdapters } from '../../channels/channel-registry.js';
import { DATA_DIR, DEFAULT_AGENT_PROVIDER, DEFAULT_MODEL } from '../../config.js';
import { readEnvFile } from '../../env.js';
import { getCodeIdentity } from '../../upgrade-state.js';
import { MIN_KEY_BYTES } from '../contract/opaque-id.js';
import type { HostSources } from './projections.js';

/**
 * Load the public-ID key, creating it on first use. It lives with the other
 * private state under `data/` (never in `.env`, images or backups exported to
 * another install: a new install derives new public IDs, which is harmless).
 */
export function loadIdKey(directory: string = path.join(DATA_DIR, 'dashboard')): Buffer {
  const file = path.join(directory, 'id-key');
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  try {
    const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
    try {
      fs.writeSync(fd, randomBytes(MIN_KEY_BYTES));
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
  }
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.mode & 0o077 || info.size < MIN_KEY_BYTES) {
    throw new Error('dashboard id key is not a private file of the expected size');
  }
  return fs.readFileSync(file);
}

function releaseIdentity(): { version: string; revision: string } | null {
  const manifest = process.env.NANOCLAW_RELEASE_MANIFEST;
  try {
    if (manifest) {
      const parsed = JSON.parse(fs.readFileSync(manifest, 'utf8')) as { version?: unknown; revision?: unknown };
      if (typeof parsed.version === 'string' && typeof parsed.revision === 'string') {
        return { version: parsed.version, revision: parsed.revision };
      }
      return null;
    }
    const code = getCodeIdentity();
    return { version: code.version, revision: code.commit };
  } catch {
    return null;
  }
}

export function liveHostSources(idKey: Buffer): HostSources {
  const endpoint = process.env.OPENCODE_BASE_URL || readEnvFile(['OPENCODE_BASE_URL']).OPENCODE_BASE_URL;
  return {
    idKey,
    channels: () =>
      getActiveAdapters().map((adapter) => ({
        key: adapter.instance ?? adapter.channelType,
        channelType: adapter.channelType,
        connected: adapter.isConnected(),
      })),
    release: releaseIdentity,
    defaults: { provider: DEFAULT_AGENT_PROVIDER, model: DEFAULT_MODEL, endpointConfigured: Boolean(endpoint) },
    now: () => new Date(),
  };
}
