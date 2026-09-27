/**
 * HTTP over a Unix socket between the dashboard service and the host (D1).
 *
 * The socket lives in its own directory, which is the only thing mounted into
 * the dashboard container: no network listener exists, so agent containers
 * and other services cannot reach it, and access is controlled by file
 * permissions (socket 0660, the directory's group is the dashboard's). It is
 * off unless `NANOCLAW_DASHBOARD_ADMIN_SOCKET` is set.
 *
 * Framing is deliberately strict: JSON only, small bodies, short timeouts,
 * `no-store` on every response. Logs name the endpoint and status, never the
 * path, query or body.
 */
import fs from 'fs';
import http from 'http';
import path from 'path';

import { log } from '../../log.js';
import { handleAdminRequest, type AdminResponse } from './boundary.js';
import type { HostSources } from './projections.js';

export const MAX_BODY_BYTES = 64 * 1024;
const REQUEST_TIMEOUT_MS = 15_000;
const METHODS = new Set(['GET', 'POST', 'DELETE']);

const SECURITY_HEADERS = {
  'cache-control': 'no-store',
  'content-type': 'application/json; charset=utf-8',
  'x-content-type-options': 'nosniff',
};

let server: http.Server | null = null;

class FramingError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}

function readBody(request: http.IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    // Listen for errors first: a refused upload is cut off, and the aborted
    // stream must not surface as an unhandled error.
    request.on('error', () => reject(new FramingError(400, 'invalid_request')));
    const declared = Number(request.headers['content-length'] ?? 0);
    if (declared > MAX_BODY_BYTES) return reject(new FramingError(413, 'payload_too_large'));
    const chunks: Buffer[] = [];
    let size = 0;
    request.on('data', (chunk: Buffer) => {
      size += chunk.length;
      // Past the limit: stop keeping data, answer 413, close after the reply.
      if (size > MAX_BODY_BYTES) reject(new FramingError(413, 'payload_too_large'));
      else chunks.push(chunk);
    });
    request.on('end', () => {
      if (size === 0) return resolve(undefined);
      const type = String(request.headers['content-type'] ?? '');
      if (!/^application\/json(?:;\s*charset=utf-8)?$/i.test(type)) {
        return reject(new FramingError(415, 'unsupported_media_type'));
      }
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch {
        reject(new FramingError(400, 'invalid_request'));
      }
    });
  });
}

function send(response: http.ServerResponse, result: Pick<AdminResponse, 'status' | 'body'>): void {
  const payload = result.status === 204 ? '' : JSON.stringify(result.body);
  const close = result.status === 413 ? { connection: 'close' } : {};
  response.writeHead(result.status, { ...SECURITY_HEADERS, ...close, 'content-length': Buffer.byteLength(payload) });
  response.end(payload);
  if (result.status === 413) response.on('finish', () => response.req.socket.destroy());
}

async function onRequest(
  request: http.IncomingMessage,
  response: http.ServerResponse,
  sources: HostSources,
): Promise<void> {
  try {
    const method = request.method ?? '';
    if (!METHODS.has(method)) throw new FramingError(405, 'method_not_allowed');
    const body = await readBody(request);
    const result = await handleAdminRequest({ method, target: request.url ?? '', body }, sources);
    log.info('Dashboard admin request', { endpoint: result.endpoint, status: result.status });
    send(response, result);
  } catch (error) {
    const framing = error instanceof FramingError;
    if (!response.headersSent) {
      send(response, {
        status: framing ? error.status : 500,
        body: { error: { code: framing ? error.code : 'internal_error', request_id: 'req_0000000000000000' } },
      });
    }
  }
}

/** Refuse a socket directory anyone else can write to or list. */
function checkDirectory(socketPath: string): void {
  const directory = path.dirname(socketPath);
  const info = fs.lstatSync(directory);
  if (!info.isDirectory() || info.mode & 0o007) {
    throw new Error('dashboard admin socket directory must be a directory closed to others (mode 07x0)');
  }
}

export async function startDashboardAdminSocket(socketPath: string, sources: HostSources): Promise<void> {
  checkDirectory(socketPath);
  if (fs.existsSync(socketPath)) {
    if (!fs.lstatSync(socketPath).isSocket()) throw new Error('dashboard admin socket path is not a socket');
    fs.unlinkSync(socketPath);
  }
  const instance = http.createServer((request, response) => void onRequest(request, response, sources));
  instance.requestTimeout = REQUEST_TIMEOUT_MS;
  instance.headersTimeout = REQUEST_TIMEOUT_MS;
  instance.maxHeadersCount = 32;
  server = instance;
  await new Promise<void>((resolve, reject) => {
    instance.once('error', reject);
    instance.listen(socketPath, () => {
      fs.chmodSync(socketPath, 0o660);
      log.info('Dashboard admin socket listening');
      resolve();
    });
  });
}

export async function stopDashboardAdminSocket(): Promise<void> {
  const current = server;
  server = null;
  if (!current) return;
  await new Promise<void>((resolve) => current.close(() => resolve()));
}
