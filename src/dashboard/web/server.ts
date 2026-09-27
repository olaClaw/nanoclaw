/**
 * The dashboard web service (D2): authentication in front of the host
 * boundary.
 *
 * It runs in its own container with no Docker socket, no `ncl.sock`, no
 * NanoClaw data: only its private state directory and the directory holding
 * the admin socket. Every request goes through the same steps:
 *
 * 1. match a contract endpoint (anything else is `not_found`);
 * 2. same-origin checks: `Sec-Fetch-Site` never cross-site, and `Origin`
 *    equal to the configured origin on every state change;
 * 3. the endpoint's policy (./../contract/authorization.ts): session,
 *    reauth window, CSRF token, per-session rate limit;
 * 4. local handling (health, login, session, logout, reauth), or forwarding
 *    to the host boundary, whose answer is validated again here;
 * 5. an audit line for state-changing endpoints.
 *
 * Responses are JSON with `no-store` and a strict CSP. Errors are a code and
 * a request ID. Nothing here logs bodies, cookies, paths or values.
 */
import { randomBytes } from 'crypto';
import http from 'http';

import { PATH_PARAMS, errorResponse, matchEndpoint, type Endpoint } from '../contract/api.js';
import { policyFor, type RateLimitClass } from '../contract/authorization.js';
import { validate } from '../contract/schema.js';
import { DEFAULT_PARAMS, hashPassword, needsRehash, verifyPassword, type ScryptParams } from './password.js';
import { LoginThrottle, SessionStore, type Session } from './sessions.js';
import type { DashboardState } from './state.js';
import { APP_CSS, APP_JS, INDEX_HTML, UI_CSP } from './ui.js';

export const COOKIE = '__Host-nanoclaw_session';
export const MAX_BODY_BYTES = 64 * 1024;

const HEADERS: Readonly<Record<string, string>> = {
  'cache-control': 'no-store',
  'content-type': 'application/json; charset=utf-8',
  'content-security-policy': "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
  'cross-origin-opener-policy': 'same-origin',
  'cross-origin-resource-policy': 'same-origin',
  'referrer-policy': 'no-referrer',
  'strict-transport-security': 'max-age=31536000',
  'x-content-type-options': 'nosniff',
  'x-frame-options': 'DENY',
};

/** Requests per minute and session. Login and reauth use the password throttle instead. */
const RATE_LIMITS: Readonly<Record<RateLimitClass, number>> = { read: 120, mutation: 20, login: 20, probe: 60 };

export type Forward = (method: string, target: string, body: unknown) => Promise<{ status: number; body: unknown }>;

/** Endpoints answered by the root-side operations service, when one is configured. */
export const OPS_ENDPOINTS = new Set([
  'releases',
  'backups',
  'job',
  'update',
  'backup_create',
  'backup_verify',
  'backup_key',
  'backup_key_saved',
  'backup_export',
  'import_preflight',
  'import_apply',
]);

export interface DashboardConfig {
  state: DashboardState;
  /** Exact origin the browser uses, e.g. `https://panel.example.invalid`. */
  origin: string;
  /** The host boundary. */
  forward: Forward;
  /** The operations service; without it, its endpoints stay with `forward` (`not_implemented`). */
  opsForward?: Forward;
  now?: () => number;
  scrypt?: ScryptParams;
}

class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly headers: Record<string, string> = {},
  ) {
    super(code);
  }
}

interface Reply {
  status: number;
  body: unknown;
  headers?: Record<string, string>;
}

const requestId = (): string => `req_${randomBytes(8).toString('hex')}`;

function cookieToken(request: http.IncomingMessage): string | undefined {
  for (const part of (request.headers.cookie ?? '').split(';')) {
    const [name, ...rest] = part.trim().split('=');
    if (name === COOKIE) return rest.join('=');
  }
  return undefined;
}

const sessionCookie = (token: string): string => `${COOKIE}=${token}; Path=/; Secure; HttpOnly; SameSite=Strict`;
const clearedCookie = `${COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0`;

function readBody(request: http.IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    // Listen for errors first: a refused upload is cut off, and the aborted
    // stream must not surface as an unhandled error.
    request.on('error', () => reject(new HttpError(400, 'invalid_request')));
    if (Number(request.headers['content-length'] ?? 0) > MAX_BODY_BYTES) {
      reject(new HttpError(413, 'payload_too_large'));
      return;
    }
    const chunks: Buffer[] = [];
    let size = 0;
    request.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) reject(new HttpError(413, 'payload_too_large'));
      else chunks.push(chunk);
    });
    request.on('end', () => {
      if (size === 0) return resolve(undefined);
      if (!/^application\/json(?:;\s*charset=utf-8)?$/i.test(String(request.headers['content-type'] ?? ''))) {
        return reject(new HttpError(415, 'unsupported_media_type'));
      }
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch {
        reject(new HttpError(400, 'invalid_request'));
      }
    });
  });
}

export function createDashboardServer(config: DashboardConfig): http.Server {
  const now = config.now ?? Date.now;
  const sessions = new SessionStore(now);
  const throttle = new LoginThrottle(config.state, now);
  const scrypt = config.scrypt ?? DEFAULT_PARAMS;
  const windows = new Map<string, { start: number; count: number }>();

  const generation = (): number => config.state.admin()?.generation ?? 0;

  function limit(key: string, rateClass: RateLimitClass): void {
    const at = now();
    const bucket = windows.get(`${rateClass}:${key}`);
    if (!bucket || at - bucket.start >= 60_000) {
      windows.set(`${rateClass}:${key}`, { start: at, count: 1 });
      if (windows.size > 1024) windows.clear();
      return;
    }
    bucket.count += 1;
    if (bucket.count > RATE_LIMITS[rateClass]) throw new HttpError(429, 'rate_limited', { 'retry-after': '60' });
  }

  /** Check the password under the global throttle; uniform failure whatever the reason. */
  async function checkPassword(password: string): Promise<void> {
    const wait = throttle.waitMs();
    if (wait > 0) throw new HttpError(429, 'login_throttled', { 'retry-after': String(Math.ceil(wait / 1000)) });
    const admin = config.state.admin();
    if (!(await verifyPassword(password, admin?.password ?? null))) {
      throttle.failure();
      throw new HttpError(401, 'invalid_credentials');
    }
    throttle.success();
    if (admin && needsRehash(admin.password, scrypt))
      config.state.setPassword(await hashPassword(password, scrypt), new Date(now()));
  }

  async function local(
    endpoint: Endpoint,
    session: Session | null,
    token: string | undefined,
    body: unknown,
  ): Promise<Reply> {
    switch (endpoint.name) {
      case 'health':
        return { status: 200, body: { status: 'ok' } };
      case 'login': {
        await checkPassword((body as { password: string }).password);
        sessions.destroy(token);
        const created = sessions.create(generation());
        return {
          status: 200,
          body: sessions.view(created.session),
          headers: { 'set-cookie': sessionCookie(created.token) },
        };
      }
      case 'session':
        return { status: 200, body: sessions.view(session!) };
      case 'logout':
        sessions.destroy(token);
        return { status: 204, body: undefined, headers: { 'set-cookie': clearedCookie } };
      case 'reauth':
        await checkPassword((body as { password: string }).password);
        sessions.markReauth(session!);
        return { status: 200, body: sessions.view(session!) };
      default:
        throw new HttpError(500, 'internal_error');
    }
  }

  async function handle(request: http.IncomingMessage): Promise<{ reply: Reply; endpoint: Endpoint | null }> {
    const method = request.method ?? '';
    const url = new URL(request.url ?? '/', 'http://dashboard.invalid');
    const found = matchEndpoint(method, url.pathname);
    if (found === 'not_found') throw new HttpError(404, 'not_found');
    if (found === 'method_not_allowed') throw new HttpError(405, 'method_not_allowed');
    const { endpoint, params } = found;
    const policy = policyFor(endpoint.name);

    if (request.headers['sec-fetch-site'] === 'cross-site') throw new HttpError(403, 'cross_site_refused');
    const origin = request.headers.origin;
    if (policy.origin ? origin !== config.origin : origin !== undefined && origin !== config.origin) {
      throw new HttpError(403, 'origin_refused');
    }
    for (const [name, value] of Object.entries(params)) {
      if (validate(PATH_PARAMS[name], value).length) throw new HttpError(404, 'not_found');
    }

    const token = cookieToken(request);
    let session: Session | null = null;
    if (policy.auth !== 'anonymous') {
      session = sessions.get(token, generation());
      if (!session) throw new HttpError(401, 'unauthenticated');
      if (policy.auth === 'reauth' && !sessions.hasReauth(session)) throw new HttpError(403, 'reauth_required');
      if (policy.csrf && !sessions.csrfMatches(session, request.headers['x-csrf-token'] as string | undefined)) {
        throw new HttpError(403, 'csrf_failed');
      }
    }
    limit(session ? session.csrf : 'anonymous', policy.rateLimit);

    const body = await readBody(request);
    if (endpoint.request === null ? body !== undefined : validate(endpoint.request, body).length) {
      if (endpoint.name !== 'import_preflight') throw new HttpError(400, 'invalid_request');
    }

    if (['health', 'login', 'session', 'logout', 'reauth'].includes(endpoint.name)) {
      return { reply: await local(endpoint, session, token, body), endpoint };
    }

    const target = config.opsForward && OPS_ENDPOINTS.has(endpoint.name) ? config.opsForward : config.forward;
    const upstream = await target(method, url.pathname + url.search, body).catch((error: unknown) => {
      // The operations service is optional: when it is not running, say so.
      if (target !== config.forward && error instanceof HttpError && error.code === 'upstream_unavailable') {
        throw new HttpError(503, 'ops_unavailable');
      }
      throw error;
    });
    const schema = upstream.status < 300 ? endpoint.response : errorResponse;
    const fits =
      schema === null
        ? upstream.body === undefined || upstream.body === null
        : validate(schema, upstream.body).length === 0;
    if (!fits) throw new HttpError(502, 'upstream_invalid');
    return { reply: { status: upstream.status, body: schema === null ? undefined : upstream.body }, endpoint };
  }

  return http.createServer((request, response) => {
    const asset = request.method === 'GET' || request.method === 'HEAD' ? uiAsset(request.url) : null;
    if (asset) {
      response.writeHead(200, {
        ...HEADERS,
        'content-security-policy': UI_CSP,
        'content-type': asset.type,
        'content-length': Buffer.byteLength(asset.body),
      });
      response.end(request.method === 'HEAD' ? undefined : asset.body);
      return;
    }
    const id = requestId();
    const send = (reply: Reply, endpoint: Endpoint | null): void => {
      const payload = reply.status === 204 || reply.body === undefined ? '' : JSON.stringify(reply.body);
      // A cut-off upload leaves the connection unusable: say so, so clients do not reuse it.
      const close = reply.status === 413 ? { connection: 'close' } : {};
      response.writeHead(reply.status, {
        ...HEADERS,
        ...(reply.headers ?? {}),
        ...close,
        'content-length': Buffer.byteLength(payload),
      });
      response.end(payload);
      if (endpoint && policyFor(endpoint.name).audit) {
        try {
          config.state.audit({
            at: new Date(now()).toISOString(),
            endpoint: endpoint.name,
            status: reply.status,
            request_id: id,
          });
        } catch {
          // An audit write failure must not turn a completed action into an error.
        }
      }
    };
    handle(request).then(
      ({ reply, endpoint }) => send(reply, endpoint),
      (error: unknown) => {
        const known = error instanceof HttpError;
        const body = { error: { code: known ? error.code : 'internal_error', request_id: id } };
        if (validate(errorResponse, body).length) body.error.code = 'internal_error';
        const found = matchEndpoint(
          request.method ?? '',
          new URL(request.url ?? '/', 'http://dashboard.invalid').pathname,
        );
        send(
          { status: known ? error.status : 500, body, headers: known ? error.headers : {} },
          typeof found === 'object' ? found.endpoint : null,
        );
        if (known && error.status === 413) response.on('finish', () => request.socket.destroy());
      },
    );
  });
}

const UI_ASSETS: Readonly<Record<string, { type: string; body: string }>> = {
  '/': { type: 'text/html; charset=utf-8', body: INDEX_HTML },
  '/app.js': { type: 'text/javascript; charset=utf-8', body: APP_JS },
  '/app.css': { type: 'text/css; charset=utf-8', body: APP_CSS },
};

/** The static UI files; no query strings, no other paths. */
function uiAsset(url: string | undefined): { type: string; body: string } | null {
  return Object.hasOwn(UI_ASSETS, url ?? '') ? UI_ASSETS[url!] : null;
}

/** Forward to the host boundary over its Unix socket. */
export function socketForward(socketPath: string, timeoutMs = 20_000): Forward {
  return (method, target, body) =>
    new Promise((resolve, reject) => {
      const payload = body === undefined ? undefined : JSON.stringify(body);
      const request = http.request(
        {
          socketPath,
          method,
          path: target,
          timeout: timeoutMs,
          headers:
            payload === undefined
              ? {}
              : { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) },
        },
        (response) => {
          const chunks: Buffer[] = [];
          let size = 0;
          response.on('data', (chunk: Buffer) => {
            size += chunk.length;
            if (size > 4 * 1024 * 1024) response.destroy();
            else chunks.push(chunk);
          });
          response.on('end', () => {
            try {
              const text = Buffer.concat(chunks).toString('utf8');
              resolve({ status: response.statusCode ?? 502, body: text ? JSON.parse(text) : undefined });
            } catch {
              reject(new HttpError(502, 'upstream_invalid'));
            }
          });
          response.on('error', () => reject(new HttpError(502, 'upstream_unavailable')));
        },
      );
      request.on('timeout', () => request.destroy());
      request.on('error', () => reject(new HttpError(502, 'upstream_unavailable')));
      if (payload !== undefined) request.write(payload);
      request.end();
    });
}
