/**
 * Dashboard API v1: resource schemas and the endpoint list.
 *
 * Normative source for docs/dashboard/api-contract.md. Each endpoint names its
 * request and response schema; the host boundary validates both, and the
 * authorization policy for each endpoint lives in ./authorization.ts.
 *
 * Nothing here is an internal identifier, path, URL, chat name, platform id,
 * credential or free text from the runtime. Agent labels are the one private
 * value, shown only to the authenticated administrator.
 */
import {
  array,
  bool,
  confirmed,
  int,
  minuteTimestamp,
  nullable,
  object,
  oneOf,
  str,
  timestamp,
  type Infer,
  type Schema,
} from './schema.js';

// ── Identifiers ──

/** Public IDs are HMAC-derived (./opaque-id.ts), never the internal row ID. */
export const PUBLIC_ID_PREFIXES = {
  agent: 'agt',
  channel: 'chn',
  session: 'ses',
  backup: 'bkp',
} as const;
export type PublicIdKind = keyof typeof PUBLIC_ID_PREFIXES;

const publicId = (kind: PublicIdKind) => str(36, new RegExp(`^${PUBLIC_ID_PREFIXES[kind]}_[0-9a-f]{32}$`));
export const agentId = publicId('agent');
export const channelId = publicId('channel');
export const sessionId = publicId('session');
export const backupId = publicId('backup');
/** Jobs get a random ID when created, so no derivation is needed. */
export const jobId = str(20, /^job_[0-9a-f]{16}$/);
export const preflightId = str(20, /^pfl_[0-9a-f]{16}$/);
export const requestId = str(20, /^req_[0-9a-f]{16}$/);

/** Stable machine codes: errors, failure categories, alert and reason codes. */
export const code = str(64, /^[a-z][a-z0-9_]{0,63}$/);
export const revision = str(40, /^[0-9a-f]{40}$/);
export const version = str(32, /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/);
/** Provider, model and channel type names are identifiers, not free text. */
export const providerName = str(32, /^[a-z][a-z0-9-]{0,31}$/);
/** Model IDs like `vendor/model:tag`; never a URL or an address. */
export const modelName = str(128, /^(?!.*\/\/)(?!.*\d+\.\d+\.\d+\.\d+)[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$/);
export const channelType = str(32, /^[a-z][a-z0-9-]{0,31}$/);
export const serviceName = str(32, /^[a-z][a-z0-9-]{0,31}$/);
/** Private administrator-visible label; the projection trims and bounds it. */
export const agentLabel = str(64, /^\S(?:.{0,62}\S)?$/u);

const MAX_PAGE = 200;
const COUNT = int(0, 1_000_000);

const page = <S extends Schema>(item: S) =>
  object({ items: array(item, MAX_PAGE), next_cursor: nullable(str(64, /^[A-Za-z0-9_-]{1,64}$/)) });

// ── Errors and jobs ──

export const errorResponse = object({ error: object({ code, request_id: requestId }) });

export const JOB_KINDS = [
  'restart_agent',
  'channel_state',
  'model_settings_apply',
  'update',
  'backup_create',
  'backup_verify',
  'backup_export',
  'import_preflight',
  'import_apply',
] as const;

/** Same outcomes as the release-update job state (scripts/compose-release-update.py). */
export const JOB_OUTCOMES = [
  'running',
  'preflight_ok',
  'succeeded',
  'failed',
  'rolled_back',
  'rollback_failed',
  'interrupted',
] as const;

export const ROLLBACK_STATES = ['not_needed', 'healthy', 'failed_manual_recovery_needed'] as const;

export const job = object({
  id: jobId,
  kind: oneOf(...JOB_KINDS),
  phase: code,
  outcome: oneOf(...JOB_OUTCOMES),
  failure_category: nullable(code),
  rollback: nullable(oneOf(...ROLLBACK_STATES)),
  release: nullable(
    object({ from_revision: nullable(revision), to_revision: nullable(revision), to_version: nullable(version) }),
  ),
  /** The backup a `backup_create` job produced, once known. */
  backup: nullable(backupId),
  phases: array(object({ phase: code, at: timestamp }), 32),
  started_at: timestamp,
  updated_at: timestamp,
  finished_at: nullable(timestamp),
});

export const jobAccepted = object({ job: object({ id: jobId, kind: oneOf(...JOB_KINDS) }) });

// ── Session (authentication) ──

const password = str(1024, /^.{1,1024}$/su);
export const loginRequest = object({ password });
export const sessionState = object({
  expires_at: timestamp,
  idle_expires_at: timestamp,
  reauth_expires_at: nullable(timestamp),
  csrf_token: str(64, /^[A-Za-z0-9_-]{43}$/),
});
export const reauthRequest = object({ password });
export const health = object({ status: oneOf('ok') });

// ── Read-only resources ──

const SERVICE_STATES = oneOf('healthy', 'unhealthy', 'stopped', 'unknown');
const REACHABILITY = oneOf('reachable', 'unreachable', 'unknown');

export const overview = object({
  release: object({ version, revision }),
  checked_at: timestamp,
  services: array(object({ name: serviceName, state: SERVICE_STATES }), 16),
  channels: object({ connected: COUNT, total: COUNT }),
  llm: object({ state: REACHABILITY }),
  alerts: array(object({ code, severity: oneOf('info', 'warning', 'critical') }), 50),
});

const agentSummaryFields = {
  id: agentId,
  label: agentLabel,
  provider: nullable(providerName),
  model: nullable(modelName),
  state: oneOf('running', 'idle', 'stopped'),
  sessions: object({ active: COUNT, total: COUNT }),
};
export const agentSummary = object(agentSummaryFields);
export const agentList = page(agentSummary);

export const agentDetail = object({
  ...agentSummaryFields,
  created_at: nullable(timestamp),
  container: object({
    state: oneOf('running', 'stopped'),
    image: object({ derived: bool, current: bool, release_revision: nullable(revision) }),
  }),
  capabilities: object({
    cli_scope: oneOf('disabled', 'group', 'global'),
    skills: oneOf('all', 'selected'),
    packages: COUNT,
    mcp_servers: COUNT,
    additional_mounts: COUNT,
  }),
});

export const channelList = page(
  object({
    id: channelId,
    type: channelType,
    state: oneOf('connected', 'disconnected', 'unknown'),
    chats: COUNT,
  }),
);

export const modelSettings = object({
  mode: oneOf('local', 'external', 'mixed', 'unconfigured'),
  provider: nullable(providerName),
  model: nullable(modelName),
  endpoint_status: object({ configured: bool, state: REACHABILITY }),
  agents: object({ total: COUNT, matching: COUNT }),
});

export const sessionList = page(
  object({
    id: sessionId,
    agent: agentId,
    state: oneOf('active', 'closed'),
    container: oneOf('running', 'idle', 'stopped'),
    last_activity: nullable(minuteTimestamp),
  }),
);

export const releases = object({
  installed: object({ version, revision }),
  candidate: nullable(object({ version, revision, verified: bool })),
  last_update: nullable(job),
});

export const backupList = page(
  object({
    id: backupId,
    created_at: timestamp,
    release_revision: revision,
    size_bytes: int(0, Number.MAX_SAFE_INTEGER),
    verification: oneOf('verified', 'unverified', 'failed'),
    exportable: bool,
    /** The backup's key is still on the server, waiting to be saved by the operator. */
    key_on_host: bool,
  }),
);

// ── Operation requests ──

export const empty = object({});
export const restartAgentRequest = object({ confirm: confirmed });
export const channelStateRequest = object({ enabled: bool, confirm: confirmed });

/**
 * Model settings apply to every agent and to the default for new ones.
 * `endpoint` is write-only: accepted here, never returned. SSRF rules for it
 * are part of D3a; the contract only bounds its shape.
 */
export const modelPreflightRequest = object({
  mode: oneOf('local', 'external'),
  provider: providerName,
  model: modelName,
  endpoint: nullable(str(2048, /^https?:\/\/[^\s]+$/)),
});
export const modelPreflight = object({
  preflight_id: preflightId,
  ready: bool,
  leaves_lan: bool,
  agents: array(object({ id: agentId, ready: bool, reason: nullable(code) }), MAX_PAGE),
  sessions_to_restart: COUNT,
  expires_at: timestamp,
});
export const modelApplyRequest = object({ preflight_id: preflightId, confirm: confirmed });

/** Secrets are input-only: the response says whether one is set, never what it is. */
export const secretKind = str(32, /^[a-z][a-z0-9_]{0,31}$/);
export const secretRequest = object({ action: oneOf('set', 'revoke'), value: nullable(str(8192, /^\S+$/)) });
export const secretState = object({ kind: secretKind, configured: bool, rotated_at: nullable(timestamp) });

export const updateRequest = object({ release_revision: revision, confirm: confirmed });

/**
 * A backup's key, shown once so the operator can store it in a password
 * manager; `backup_key_saved` then removes it from the server. The one
 * response in the contract that carries a secret, by the operator's choice.
 */
export const backupKey = object({ backup: backupId, key: str(64, /^[0-9a-f]{64}$/) });
export const backupKeyState = object({ backup: backupId, key_on_host: bool });
export const backupCreateRequest = object({ confirm: confirmed });

export type Overview = Infer<typeof overview>;
export type AgentSummary = Infer<typeof agentSummary>;
export type AgentDetail = Infer<typeof agentDetail>;
export type Job = Infer<typeof job>;

// ── Endpoints ──

export type Method = 'GET' | 'POST' | 'DELETE';

export interface Endpoint {
  readonly name: string;
  readonly method: Method;
  /** Path parameters are `{name}` and must be one of PATH_PARAMS. */
  readonly path: string;
  readonly request: Schema | null;
  /** `null` means 204 No Content. */
  readonly response: Schema | null;
  /** `draft` endpoints depend on a later epic's design (export/import format). */
  readonly status: 'v1' | 'draft';
  readonly epic: `D${number}` | `D${number}a`;
}

export const PATH_PARAMS: Readonly<Record<string, Schema>> = {
  agent: agentId,
  channel: channelId,
  backup: backupId,
  job: jobId,
  kind: secretKind,
};

export const ENDPOINTS: readonly Endpoint[] = [
  { name: 'health', method: 'GET', path: '/api/v1/health', request: null, response: health, status: 'v1', epic: 'D2' },
  {
    name: 'login',
    method: 'POST',
    path: '/api/v1/session',
    request: loginRequest,
    response: sessionState,
    status: 'v1',
    epic: 'D2',
  },
  {
    name: 'session',
    method: 'GET',
    path: '/api/v1/session',
    request: null,
    response: sessionState,
    status: 'v1',
    epic: 'D2',
  },
  {
    name: 'logout',
    method: 'DELETE',
    path: '/api/v1/session',
    request: null,
    response: null,
    status: 'v1',
    epic: 'D2',
  },
  {
    name: 'reauth',
    method: 'POST',
    path: '/api/v1/session/reauth',
    request: reauthRequest,
    response: sessionState,
    status: 'v1',
    epic: 'D2',
  },

  {
    name: 'overview',
    method: 'GET',
    path: '/api/v1/overview',
    request: null,
    response: overview,
    status: 'v1',
    epic: 'D3',
  },
  {
    name: 'agents',
    method: 'GET',
    path: '/api/v1/agents',
    request: null,
    response: agentList,
    status: 'v1',
    epic: 'D3',
  },
  {
    name: 'agent',
    method: 'GET',
    path: '/api/v1/agents/{agent}',
    request: null,
    response: agentDetail,
    status: 'v1',
    epic: 'D3',
  },
  {
    name: 'channels',
    method: 'GET',
    path: '/api/v1/channels',
    request: null,
    response: channelList,
    status: 'v1',
    epic: 'D3',
  },
  {
    name: 'model_settings',
    method: 'GET',
    path: '/api/v1/model-settings',
    request: null,
    response: modelSettings,
    status: 'v1',
    epic: 'D3',
  },
  {
    name: 'sessions',
    method: 'GET',
    path: '/api/v1/sessions',
    request: null,
    response: sessionList,
    status: 'v1',
    epic: 'D3',
  },
  {
    name: 'releases',
    method: 'GET',
    path: '/api/v1/releases',
    request: null,
    response: releases,
    status: 'v1',
    epic: 'D3',
  },
  {
    name: 'backups',
    method: 'GET',
    path: '/api/v1/backups',
    request: null,
    response: backupList,
    status: 'v1',
    epic: 'D3',
  },
  { name: 'job', method: 'GET', path: '/api/v1/jobs/{job}', request: null, response: job, status: 'v1', epic: 'D3' },

  {
    name: 'restart_agent',
    method: 'POST',
    path: '/api/v1/agents/{agent}/restart',
    request: restartAgentRequest,
    response: jobAccepted,
    status: 'v1',
    epic: 'D6',
  },
  {
    name: 'channel_state',
    method: 'POST',
    path: '/api/v1/channels/{channel}/state',
    request: channelStateRequest,
    response: jobAccepted,
    status: 'v1',
    epic: 'D6',
  },
  {
    name: 'secret',
    method: 'POST',
    path: '/api/v1/secrets/{kind}',
    request: secretRequest,
    response: secretState,
    status: 'v1',
    epic: 'D6',
  },
  {
    name: 'model_preflight',
    method: 'POST',
    path: '/api/v1/model-settings/preflight',
    request: modelPreflightRequest,
    response: modelPreflight,
    status: 'v1',
    epic: 'D3a',
  },
  {
    name: 'model_apply',
    method: 'POST',
    path: '/api/v1/model-settings/apply',
    request: modelApplyRequest,
    response: jobAccepted,
    status: 'v1',
    epic: 'D3a',
  },
  {
    name: 'update',
    method: 'POST',
    path: '/api/v1/updates',
    request: updateRequest,
    response: jobAccepted,
    status: 'v1',
    epic: 'D7',
  },
  {
    name: 'backup_create',
    method: 'POST',
    path: '/api/v1/backups',
    request: backupCreateRequest,
    response: jobAccepted,
    status: 'v1',
    epic: 'D4',
  },
  {
    name: 'backup_verify',
    method: 'POST',
    path: '/api/v1/backups/{backup}/verify',
    request: empty,
    response: jobAccepted,
    status: 'v1',
    epic: 'D4',
  },
  {
    name: 'backup_key',
    method: 'POST',
    path: '/api/v1/backups/{backup}/key',
    request: backupCreateRequest,
    response: backupKey,
    status: 'v1',
    epic: 'D4',
  },
  {
    name: 'backup_key_saved',
    method: 'POST',
    path: '/api/v1/backups/{backup}/key/saved',
    request: backupCreateRequest,
    response: backupKeyState,
    status: 'v1',
    epic: 'D4',
  },
  // Export/import payloads (one-time key delivery, upload framing) are settled in D5.
  {
    name: 'backup_export',
    method: 'POST',
    path: '/api/v1/backups/{backup}/export',
    request: backupCreateRequest,
    response: jobAccepted,
    status: 'draft',
    epic: 'D5',
  },
  {
    name: 'import_preflight',
    method: 'POST',
    path: '/api/v1/imports/preflight',
    request: null,
    response: jobAccepted,
    status: 'draft',
    epic: 'D5',
  },
  {
    name: 'import_apply',
    method: 'POST',
    path: '/api/v1/imports/{job}/apply',
    request: object({ mode: oneOf('rehearsal', 'migration'), confirm: confirmed }),
    response: jobAccepted,
    status: 'draft',
    epic: 'D5',
  },
];

export function endpoint(name: string): Endpoint {
  const found = ENDPOINTS.find((item) => item.name === name);
  if (!found) throw new Error(`unknown dashboard endpoint: ${name}`);
  return found;
}

const ROUTES = ENDPOINTS.map((item) => ({
  endpoint: item,
  regex: new RegExp(`^${item.path.replace(/\{([a-z]+)\}/g, (_, name: string) => `(?<${name}>[^/]+)`)}$`),
}));

/**
 * The endpoint for a method and path, with its raw path parameters, or why
 * none matches. Parameters still need validation against PATH_PARAMS.
 */
export function matchEndpoint(
  method: string,
  path: string,
): { endpoint: Endpoint; params: Record<string, string> } | 'not_found' | 'method_not_allowed' {
  let pathMatched = false;
  for (const route of ROUTES) {
    const found = route.regex.exec(path);
    if (!found) continue;
    pathMatched = true;
    if (route.endpoint.method === method) return { endpoint: route.endpoint, params: { ...found.groups } };
  }
  return pathMatched ? 'method_not_allowed' : 'not_found';
}
