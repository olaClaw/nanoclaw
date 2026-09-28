/**
 * Live host state for the dashboard projections, and the per-install key for
 * public IDs. Kept apart from the projections so tests can inject both.
 */
import { randomBytes } from 'crypto';
import fs from 'fs';
import path from 'path';

import { getActiveAdapters } from '../../channels/channel-registry.js';
import { restartAgentGroupContainers } from '../../container-restart.js';
import { DATA_DIR } from '../../config.js';
import { readEnvFile } from '../../env.js';
import { defaultAgentProvider, defaultModel, readModelSettings } from '../../model-settings.js';
import { getCodeIdentity } from '../../upgrade-state.js';
import { MIN_KEY_BYTES } from '../contract/opaque-id.js';
import { getGatewayProvider } from '../../gateway-providers/index.js';
import { liveEndpointDeps, probeEndpoint } from './model-endpoint.js';
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

const REACHABILITY_TTL_MS = 60_000;

/** The OpenCode endpoint and model containers start with: the panel's choice, else `.env`. */
function openCodeTarget(): { endpoint: string; model: string } {
  const settings = readModelSettings();
  // An external profile has no LAN endpoint to watch (Claude has no OpenCode model either).
  if (settings) {
    return settings.provider === 'opencode'
      ? { endpoint: settings.profile === 'local' ? settings.endpoint! : '', model: settings.model }
      : { endpoint: '', model: '' };
  }
  const env = readEnvFile(['OPENCODE_BASE_URL', 'OPENCODE_MODEL']);
  return {
    endpoint: process.env.OPENCODE_BASE_URL || env.OPENCODE_BASE_URL || '',
    model: process.env.OPENCODE_MODEL || env.OPENCODE_MODEL || '',
  };
}

export function liveHostSources(idKey: Buffer): HostSources {
  let reachability: { endpoint: string; at: number; state: 'reachable' | 'unreachable' | 'unknown' } | null = null;
  return {
    idKey,
    channels: () =>
      getActiveAdapters().map((adapter) => ({
        key: adapter.instance ?? adapter.channelType,
        channelType: adapter.channelType,
        connected: adapter.isConnected(),
      })),
    release: releaseIdentity,
    get defaults() {
      const target = openCodeTarget();
      return {
        provider: defaultAgentProvider(),
        model: defaultModel(),
        opencodeModel: target.model,
        endpointConfigured: Boolean(target.endpoint),
      };
    },
    now: () => new Date(),
    restartAgent: (internalId) => restartAgentGroupContainers(internalId, 'restarted from the dashboard'),
    dataDir: DATA_DIR,
    probeModel: (endpoint, model) => probeEndpoint(endpoint, model, liveEndpointDeps()),
    async probeGateway(agentGroupId, groupName, url, headers) {
      const probes = getGatewayProvider().probes;
      return probes ? probes.get({ agentGroupId, groupName, url, headers }) : null;
    },
    async endpointState() {
      const { endpoint } = openCodeTarget();
      if (!endpoint || endpoint === 'native') return 'unknown';
      if (reachability?.endpoint === endpoint && Date.now() - reachability.at < REACHABILITY_TTL_MS) {
        return reachability.state;
      }
      const probe = await probeEndpoint(endpoint, null, liveEndpointDeps(), { list: 3_000, inference: 0 });
      // Addresses the probe may not contact (public, loopback) are not "down", just unchecked.
      const state =
        probe.reason === null || probe.reason === 'endpoint_unauthorized'
          ? 'reachable'
          : ['endpoint_unreachable', 'endpoint_tls_failed', 'endpoint_invalid_response'].includes(probe.reason)
            ? 'unreachable'
            : 'unknown';
      reachability = { endpoint, at: Date.now(), state };
      return state;
    },
  };
}
