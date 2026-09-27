/**
 * Read-only projections for the dashboard API (D1).
 *
 * Each function builds one response from the central DB and live host state,
 * copying only the fields the contract allows. Nothing is passed through: raw
 * rows, JSON config columns and adapter objects stay here, and every value
 * that leaves is either counted, mapped to a closed set, or checked against
 * its identifier pattern (falling back to null or a neutral value when it
 * does not fit). The boundary validates the result against the contract as a
 * second line of defense.
 */
import { getDb } from '../../db/connection.js';
import type { AgentGroup, ContainerConfigRow, MessagingGroup, Session } from '../../types.js';
import {
  agentLabel,
  modelName,
  providerName,
  channelType as channelTypePattern,
  version as versionPattern,
  revision as revisionPattern,
  type AgentDetail,
  type AgentSummary,
  type Overview,
} from '../contract/api.js';
import { publicId } from '../contract/opaque-id.js';
import { validate, type Infer } from '../contract/schema.js';
import type { channelList, modelSettings, sessionList } from '../contract/api.js';

export const PAGE_SIZE = 50;

/** Live state the DB does not hold; injected so tests can drive it. */
export interface HostSources {
  idKey: Buffer;
  channels(): ReadonlyArray<{ key: string; channelType: string; connected: boolean }>;
  release(): { version: string; revision: string } | null;
  defaults: { provider: string; model: string; endpointConfigured: boolean };
  now(): Date;
  /** Restart the agent group's running containers; resolves to how many. */
  restartAgent(internalId: string): Promise<number>;
}

/** Providers whose model endpoint the operator configures on the LAN. */
const LOCAL_ENDPOINT_PROVIDERS = new Set(['opencode']);

function fits(schema: Parameters<typeof validate>[0], value: unknown): boolean {
  return validate(schema, value).length === 0;
}

/** Trimmed, single-line, bounded; a neutral label when nothing printable is left. */
export function safeLabel(raw: string | null | undefined): string {
  const flat = (raw ?? '')
    .replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const label = [...flat].slice(0, 64).join('').trim();
  return fits(agentLabel, label) ? label : 'Agent';
}

function identifier<S extends Parameters<typeof validate>[0]>(
  schema: S,
  raw: string | null | undefined,
): string | null {
  return raw && fits(schema, raw) ? raw : null;
}

export function isoOrNull(raw: string | null | undefined, precision: 'second' | 'minute' = 'second'): string | null {
  if (!raw) return null;
  const time = Date.parse(raw);
  if (Number.isNaN(time)) return null;
  const date = new Date(time);
  if (precision === 'minute') date.setUTCSeconds(0, 0);
  else date.setUTCMilliseconds(0);
  return date.toISOString().replace('.000Z', 'Z');
}

export function pageOf<T>(items: T[], cursor: string | null): { items: T[]; next_cursor: string | null } {
  const offset = cursor ? Number(cursor.slice(1)) : 0;
  const slice = items.slice(offset, offset + PAGE_SIZE);
  const next = offset + PAGE_SIZE < items.length ? `o${offset + PAGE_SIZE}` : null;
  return { items: slice, next_cursor: next };
}

interface AgentFacts {
  group: AgentGroup;
  config: ContainerConfigRow | undefined;
  sessions: Session[];
}

async function agentFacts(): Promise<AgentFacts[]> {
  const db = getDb();
  const groups = await db.all<AgentGroup>('SELECT * FROM agent_groups ORDER BY created_at, id');
  const configs = new Map(
    (await db.all<ContainerConfigRow>('SELECT * FROM container_configs')).map((row) => [row.agent_group_id, row]),
  );
  const sessions = await db.all<Session>('SELECT * FROM sessions');
  return groups.map((group) => ({
    group,
    config: configs.get(group.id),
    sessions: sessions.filter((session) => session.agent_group_id === group.id),
  }));
}

function effectiveProvider(facts: AgentFacts, sources: HostSources): string | null {
  return identifier(providerName, (facts.config?.provider ?? sources.defaults.provider).toLowerCase());
}

function effectiveModel(facts: AgentFacts, sources: HostSources): string | null {
  return identifier(modelName, facts.config?.model ?? (sources.defaults.model || null));
}

function summary(facts: AgentFacts, sources: HostSources): AgentSummary {
  const containers = new Set(facts.sessions.map((session) => session.container_status));
  return {
    id: publicId('agent', facts.group.id, sources.idKey),
    label: safeLabel(facts.group.name),
    provider: effectiveProvider(facts, sources),
    model: effectiveModel(facts, sources),
    state: containers.has('running') ? 'running' : containers.has('idle') ? 'idle' : 'stopped',
    sessions: {
      active: facts.sessions.filter((session) => session.status === 'active').length,
      total: facts.sessions.length,
    },
  };
}

function jsonCount(raw: string | undefined, kind: 'array' | 'object'): number {
  try {
    const value: unknown = JSON.parse(raw ?? (kind === 'array' ? '[]' : '{}'));
    if (kind === 'array') return Array.isArray(value) ? value.length : 0;
    return value && typeof value === 'object' && !Array.isArray(value) ? Object.keys(value).length : 0;
  } catch {
    return 0;
  }
}

export async function listAgents(sources: HostSources, cursor: string | null) {
  return pageOf(
    (await agentFacts()).map((facts) => summary(facts, sources)),
    cursor,
  );
}

/** The internal IDs public agent IDs are resolved against. */
export async function agentIds(): Promise<string[]> {
  return (await getDb().all<{ id: string }>('SELECT id FROM agent_groups')).map((row) => row.id);
}

export async function getAgent(sources: HostSources, internalId: string): Promise<AgentDetail | null> {
  const facts = (await agentFacts()).find((item) => item.group.id === internalId);
  if (!facts) return null;
  const base = summary(facts, sources);
  const packages = jsonCount(facts.config?.packages_apt, 'array') + jsonCount(facts.config?.packages_npm, 'array');
  const scope = facts.config?.cli_scope;
  return {
    ...base,
    created_at: isoOrNull(facts.group.created_at),
    container: {
      state: base.state === 'running' ? 'running' : 'stopped',
      // Whether the derived image is current needs the Docker daemon; the
      // operations service reports it (D4/D7). Until then: derived or not.
      image: { derived: Boolean(facts.config?.image_tag) && packages > 0, current: true, release_revision: null },
    },
    capabilities: {
      cli_scope: scope === 'disabled' || scope === 'global' ? scope : 'group',
      skills: (facts.config?.skills ?? '"all"') === '"all"' ? 'all' : 'selected',
      packages,
      mcp_servers: jsonCount(facts.config?.mcp_servers, 'object'),
      additional_mounts: jsonCount(facts.config?.additional_mounts, 'array'),
    },
  };
}

type ChannelItem = Infer<typeof channelList>['items'][number];

/** One entry per adapter instance: live adapters plus instances only known from the DB. */
async function channelFacts(sources: HostSources) {
  const chats = await getDb().all<Pick<MessagingGroup, 'channel_type' | 'instance'> & { n: number }>(
    'SELECT channel_type, instance, COUNT(*) AS n FROM messaging_groups GROUP BY channel_type, instance',
  );
  const byKey = new Map<string, { channelType: string; connected: boolean | null; chats: number }>();
  for (const adapter of sources.channels()) {
    byKey.set(adapter.key, { channelType: adapter.channelType, connected: adapter.connected, chats: 0 });
  }
  for (const row of chats) {
    const key = row.instance ?? row.channel_type;
    const entry = byKey.get(key) ?? { channelType: row.channel_type, connected: null, chats: 0 };
    entry.chats += row.n;
    byKey.set(key, entry);
  }
  return [...byKey.entries()].sort(([a], [b]) => a.localeCompare(b));
}

export async function listChannels(sources: HostSources, cursor: string | null) {
  const items: ChannelItem[] = (await channelFacts(sources)).map(([key, entry]) => ({
    id: publicId('channel', key, sources.idKey),
    type: identifier(channelTypePattern, entry.channelType) ?? 'other',
    state: entry.connected === null ? 'unknown' : entry.connected ? 'connected' : 'disconnected',
    chats: entry.chats,
  }));
  return pageOf(items, cursor);
}

type SessionItem = Infer<typeof sessionList>['items'][number];

export async function listSessions(sources: HostSources, cursor: string | null) {
  const rows = await getDb().all<Session>(
    'SELECT * FROM sessions ORDER BY (last_active IS NULL), last_active DESC, id',
  );
  const items: SessionItem[] = rows.map((row) => ({
    id: publicId('session', row.id, sources.idKey),
    agent: publicId('agent', row.agent_group_id, sources.idKey),
    state: row.status === 'closed' ? 'closed' : 'active',
    container: row.container_status === 'running' || row.container_status === 'idle' ? row.container_status : 'stopped',
    last_activity: isoOrNull(row.last_active, 'minute'),
  }));
  return pageOf(items, cursor);
}

export async function getModelSettings(sources: HostSources): Promise<Infer<typeof modelSettings>> {
  const facts = await agentFacts();
  const pairs = facts.map((item) => `${effectiveProvider(item, sources)}\0${effectiveModel(item, sources)}`);
  const counts = new Map<string, number>();
  for (const pair of pairs) counts.set(pair, (counts.get(pair) ?? 0) + 1);
  const [top] = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  const provider = identifier(providerName, sources.defaults.provider);
  const model = identifier(modelName, sources.defaults.model || null);
  const matching = pairs.filter((pair) => pair === `${provider}\0${model}`).length;
  const uniform = counts.size === 1 && top !== undefined && top[0] === `${provider}\0${model}`;
  return {
    mode:
      facts.length === 0 && !provider
        ? 'unconfigured'
        : facts.length > 0 && !uniform
          ? 'mixed'
          : provider && LOCAL_ENDPOINT_PROVIDERS.has(provider)
            ? 'local'
            : 'external',
    provider,
    model,
    // Reachability needs a probe from the host; reported by D3a.
    endpoint_status: { configured: sources.defaults.endpointConfigured, state: 'unknown' },
    agents: { total: facts.length, matching },
  };
}

export async function getOverview(sources: HostSources): Promise<Overview | null> {
  const release = sources.release();
  if (!release || !fits(versionPattern, release.version) || !fits(revisionPattern, release.revision)) return null;
  const channels = sources.channels();
  return {
    release: { version: release.version, revision: release.revision },
    checked_at: isoOrNull(sources.now().toISOString())!,
    // Other services' health needs the Docker daemon; the operations service
    // reports it (D4/D7). The host reports itself.
    services: [{ name: 'nanoclaw', state: 'healthy' }],
    channels: { connected: channels.filter((item) => item.connected).length, total: channels.length },
    llm: { state: 'unknown' },
    alerts: channels.some((item) => !item.connected) ? [{ code: 'channel_disconnected', severity: 'warning' }] : [],
  };
}
