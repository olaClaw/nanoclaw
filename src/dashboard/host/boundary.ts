/**
 * Host-side administrative boundary for the dashboard (D1).
 *
 * The dashboard service is treated as untrusted. It authenticates the
 * administrator (D2) and forwards API calls here; this module decides what
 * actually runs. A request is matched to a contract endpoint by method and
 * path, its path parameters and body are validated, public IDs are resolved
 * server-side, and the handler's result is validated against the response
 * schema before anything is returned. A result that does not fit is dropped
 * and replaced by a `contract_violation` error, so a projection bug can never
 * leak a raw value.
 *
 * Errors carry a stable code and a random request ID only.
 */
import { randomBytes } from 'crypto';

import { log } from '../../log.js';
import { PATH_PARAMS, errorResponse, matchEndpoint, type Endpoint } from '../contract/api.js';
import { resolvePublicId } from '../contract/opaque-id.js';
import { validate } from '../contract/schema.js';
import {
  agentIds,
  getAgent,
  getModelSettings,
  getOverview,
  listAgents,
  listChannels,
  listSessions,
  type HostSources,
} from './projections.js';

/** Handled by the dashboard service itself (login and sessions); never forwarded. */
export const DASHBOARD_LOCAL = new Set(['health', 'login', 'session', 'logout', 'reauth']);

const LIST_ENDPOINTS = new Set(['agents', 'channels', 'sessions']);
const CURSOR = /^o\d{1,6}$/;

export interface AdminRequest {
  method: string;
  /** Path and optional query string, e.g. `/api/v1/agents?cursor=o50`. */
  target: string;
  body: unknown;
}

export interface AdminResponse {
  status: number;
  body: unknown;
  /** Endpoint name for logs and audit; never the path (it can hold IDs). */
  endpoint: string | null;
  requestId: string;
}

class BoundaryError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}

type Handler = (context: {
  sources: HostSources;
  params: Record<string, string>;
  cursor: string | null;
  body: unknown;
}) => Promise<unknown>;

async function resolveAgent(publicAgentId: string, sources: HostSources): Promise<string> {
  const internal = resolvePublicId('agent', publicAgentId, await agentIds(), sources.idKey);
  if (!internal) throw new BoundaryError(404, 'not_found');
  return internal;
}

/** Agents with a restart in flight; a second request waits for the first to finish. */
const restarting = new Set<string>();

const HANDLERS: Readonly<Record<string, Handler>> = {
  restart_agent: async ({ sources, params }) => {
    const internal = await resolveAgent(params.agent, sources);
    if (restarting.has(internal)) throw new BoundaryError(409, 'restart_in_progress');
    restarting.add(internal);
    try {
      return { agent: params.agent, restarted: await sources.restartAgent(internal) };
    } finally {
      restarting.delete(internal);
    }
  },
  overview: async ({ sources }) => {
    const overview = await getOverview(sources);
    if (!overview) throw new BoundaryError(503, 'release_unknown');
    return overview;
  },
  agents: ({ sources, cursor }) => listAgents(sources, cursor),
  agent: async ({ sources, params }) => {
    const agent = await getAgent(sources, await resolveAgent(params.agent, sources));
    if (!agent) throw new BoundaryError(404, 'not_found');
    return agent;
  },
  channels: ({ sources, cursor }) => listChannels(sources, cursor),
  sessions: ({ sources, cursor }) => listSessions(sources, cursor),
  model_settings: ({ sources }) => getModelSettings(sources),
};

function newRequestId(): string {
  return `req_${randomBytes(8).toString('hex')}`;
}

function match(method: string, path: string): { endpoint: Endpoint; params: Record<string, string> } {
  const found = matchEndpoint(method, path);
  if (found === 'not_found') throw new BoundaryError(404, 'not_found');
  if (found === 'method_not_allowed') throw new BoundaryError(405, 'method_not_allowed');
  return found;
}

function parseTarget(target: string): { path: string; query: URLSearchParams } {
  if (typeof target !== 'string' || target.length > 512 || !target.startsWith('/')) {
    throw new BoundaryError(400, 'invalid_request');
  }
  const url = new URL(target, 'http://boundary.invalid');
  return { path: url.pathname, query: url.searchParams };
}

function cursorOf(endpoint: Endpoint, query: URLSearchParams): string | null {
  const keys = [...query.keys()];
  if (keys.length === 0) return null;
  if (!LIST_ENDPOINTS.has(endpoint.name) || keys.length !== 1 || keys[0] !== 'cursor') {
    throw new BoundaryError(400, 'invalid_query');
  }
  const cursor = query.get('cursor')!;
  if (!CURSOR.test(cursor)) throw new BoundaryError(400, 'invalid_query');
  return cursor;
}

async function route(request: AdminRequest, sources: HostSources): Promise<{ endpoint: Endpoint; body: unknown }> {
  const { path, query } = parseTarget(request.target);
  const { endpoint, params } = match(request.method, path);
  if (DASHBOARD_LOCAL.has(endpoint.name)) throw Object.assign(new BoundaryError(404, 'not_found'), { endpoint });
  for (const [name, value] of Object.entries(params)) {
    if (validate(PATH_PARAMS[name], value).length)
      throw Object.assign(new BoundaryError(404, 'not_found'), { endpoint });
  }
  const cursor = cursorOf(endpoint, query);
  if (endpoint.request === null ? request.body !== undefined : validate(endpoint.request, request.body).length) {
    throw Object.assign(new BoundaryError(400, 'invalid_request'), { endpoint });
  }
  const handler = HANDLERS[endpoint.name];
  if (!handler) throw Object.assign(new BoundaryError(501, 'not_implemented'), { endpoint });
  const body = await handler({ sources, params, cursor, body: request.body });
  if (endpoint.response === null ? body !== undefined : validate(endpoint.response, body).length) {
    throw Object.assign(new BoundaryError(500, 'contract_violation'), { endpoint });
  }
  return { endpoint, body };
}

export async function handleAdminRequest(request: AdminRequest, sources: HostSources): Promise<AdminResponse> {
  const requestId = newRequestId();
  try {
    const { endpoint, body } = await route(request, sources);
    return { status: endpoint.response === null ? 204 : 200, body, endpoint: endpoint.name, requestId };
  } catch (error) {
    const known = error instanceof BoundaryError;
    const endpoint = (error as { endpoint?: Endpoint }).endpoint?.name ?? null;
    if (!known || error.status >= 500) {
      // Category only: the error message may quote runtime values.
      log.warn('Dashboard admin request failed', {
        endpoint,
        code: known ? error.code : 'internal_error',
        request_id: requestId,
      });
    }
    const body = {
      error: { code: known ? error.code : 'internal_error', request_id: requestId },
    };
    if (validate(errorResponse, body).length) throw new Error('error response outside contract', { cause: error });
    return { status: known ? error.status : 500, body, endpoint, requestId };
  }
}
