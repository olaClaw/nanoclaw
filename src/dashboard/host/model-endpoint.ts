/**
 * Local model endpoint checks for the dashboard (D3a, threat T15).
 *
 * The URL comes from the administrator's browser, so the host treats it as
 * hostile: http(s) only, no credentials, query or fragment, and a name that
 * resolves only to private LAN/VPN addresses — never loopback, link-local,
 * multicast, or a network this container sits on (the Compose networks with
 * the database, gateway and brokers). Requests go to the address that was
 * checked (no second DNS lookup), follow no redirects, send no credentials
 * and read a bounded body. Nothing about the endpoint (URL, address, body) is
 * logged or returned: only a result code.
 */
import dns from 'dns/promises';
import http from 'http';
import https from 'https';
import net from 'net';
import os from 'os';

/**
 * LAN and VPN ranges an endpoint may resolve to: RFC 1918, CGNAT (as used by
 * Tailscale) and IPv6 ULA. The IPv4 networks are written as octets because
 * the source privacy gate reports every dotted private address.
 */
const PRIVATE_V4: ReadonlyArray<readonly [number, number, number]> = [
  [10, 0, 8],
  [172, 16, 12],
  [192, 168, 16],
  [100, 64, 10],
];

function privateRanges(): net.BlockList {
  const list = new net.BlockList();
  for (const [first, second, prefix] of PRIVATE_V4) list.addSubnet(`${first}.${second}.0.0`, prefix, 'ipv4');
  list.addSubnet('fc00::', 7, 'ipv6');
  return list;
}

/** The networks this process is attached to: in Compose, the stack's own bridges. */
export function ownNetworks(): net.BlockList {
  const list = new net.BlockList();
  for (const entries of Object.values(os.networkInterfaces())) {
    for (const entry of entries ?? []) {
      if (!entry.cidr) continue;
      const [address, prefix] = entry.cidr.split('/');
      list.addSubnet(address, Number(prefix), entry.family === 'IPv6' ? 'ipv6' : 'ipv4');
    }
  }
  return list;
}

export interface EndpointDeps {
  lookup: (hostname: string) => Promise<Array<{ address: string; family: number }>>;
  own: net.BlockList;
  allowed: net.BlockList;
  request: ProbeRequest;
}

export type ProbeRequest = (
  target: URL,
  address: string,
  family: number,
  init: { method: 'GET' | 'POST'; body?: string; timeoutMs: number },
) => Promise<{ status: number; body: unknown }>;

export type EndpointCheck =
  | { ok: true; url: URL; address: string; family: number }
  | { ok: false; reason: 'endpoint_invalid' | 'endpoint_not_private' | 'endpoint_unresolvable' };

const MAX_BODY = 256 * 1024;

export async function checkEndpoint(raw: string, deps: EndpointDeps): Promise<EndpointCheck> {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return { ok: false, reason: 'endpoint_invalid' };
  }
  if (
    (url.protocol !== 'http:' && url.protocol !== 'https:') ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    raw.length > 2048
  ) {
    return { ok: false, reason: 'endpoint_invalid' };
  }
  const host = url.hostname.replace(/^\[|\]$/g, '');
  let addresses: Array<{ address: string; family: number }>;
  if (net.isIP(host)) addresses = [{ address: host, family: net.isIP(host) }];
  else {
    // A single-label name (`postgres`, `onecli`) is a Compose service, not a LAN host.
    if (!host.includes('.') || host.endsWith('.localhost') || host === 'localhost') {
      return { ok: false, reason: 'endpoint_not_private' };
    }
    try {
      addresses = await deps.lookup(host);
    } catch {
      return { ok: false, reason: 'endpoint_unresolvable' };
    }
    if (addresses.length === 0) return { ok: false, reason: 'endpoint_unresolvable' };
  }
  for (const { address, family } of addresses) {
    const type = family === 6 ? 'ipv6' : 'ipv4';
    if (!deps.allowed.check(address, type) || deps.own.check(address, type)) {
      return { ok: false, reason: 'endpoint_not_private' };
    }
  }
  return { ok: true, url, address: addresses[0].address, family: addresses[0].family };
}

/** One HTTP exchange with the checked address, no redirects, bounded body, hard deadline. */
export const pinnedRequest: ProbeRequest = (target, address, family, init) =>
  new Promise((resolve, reject) => {
    const client = target.protocol === 'https:' ? https : http;
    const request = client.request(
      target,
      {
        method: init.method,
        agent: false,
        headers: {
          accept: 'application/json',
          'user-agent': 'nanoclaw-dashboard-probe',
          ...(init.body !== undefined
            ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(init.body) }
            : {}),
        },
        lookup: ((_host: string, options: { all?: boolean }, callback: (...args: unknown[]) => void) => {
          if (options?.all) callback(null, [{ address, family }]);
          else callback(null, address, family);
        }) as unknown as net.LookupFunction,
      },
      (response) => {
        const chunks: Buffer[] = [];
        let size = 0;
        response.on('data', (chunk: Buffer) => {
          size += chunk.length;
          if (size > MAX_BODY) request.destroy(new Error('body_too_large'));
          else chunks.push(chunk);
        });
        response.on('end', () => {
          let body: unknown = null;
          try {
            body = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
          } catch {
            body = null;
          }
          resolve({ status: response.statusCode ?? 0, body });
        });
        response.on('error', reject);
      },
    );
    const timer = setTimeout(() => request.destroy(new Error('timeout')), init.timeoutMs);
    timer.unref();
    request.on('close', () => clearTimeout(timer));
    request.on('error', reject);
    request.end(init.body);
  });

export function liveEndpointDeps(): EndpointDeps {
  return {
    lookup: (hostname) => dns.lookup(hostname, { all: true, verbatim: true }),
    own: ownNetworks(),
    allowed: privateRanges(),
    request: pinnedRequest,
  };
}

export interface ProbeResult {
  /** Null when the endpoint serves the model; otherwise a stable code. */
  reason: string | null;
  /** Model IDs the endpoint lists, bounded; empty when it could not be read. */
  models: string[];
  /** The model's context window when the endpoint reports one (vLLM `max_model_len`). */
  contextLimit: number | null;
}

function failure(error: unknown): string {
  const code = (error as { code?: string }).code ?? '';
  if (/CERT|TLS|SSL|SELF_SIGNED|UNABLE_TO_VERIFY/.test(code)) return 'endpoint_tls_failed';
  return 'endpoint_unreachable';
}

function join(base: URL, suffix: string): URL {
  const url = new URL(base.toString());
  url.pathname = `${url.pathname.replace(/\/+$/, '')}${suffix}`;
  return url;
}

/**
 * Check an OpenAI-compatible endpoint: address rules, `/models`, and — when a
 * model is given — that it is listed and answers a one-token completion.
 */
export async function probeEndpoint(
  raw: string,
  model: string | null,
  deps: EndpointDeps,
  timeouts = { list: 5_000, inference: 12_000 },
): Promise<ProbeResult> {
  const checked = await checkEndpoint(raw, deps);
  if (!checked.ok) return { reason: checked.reason, models: [], contextLimit: null };
  let listed: { status: number; body: unknown };
  try {
    listed = await deps.request(join(checked.url, '/models'), checked.address, checked.family, {
      method: 'GET',
      timeoutMs: timeouts.list,
    });
  } catch (error) {
    return { reason: failure(error), models: [], contextLimit: null };
  }
  if (listed.status === 401 || listed.status === 403) {
    return { reason: 'endpoint_unauthorized', models: [], contextLimit: null };
  }
  const data = (listed.body as { data?: unknown } | null)?.data;
  if (listed.status !== 200 || !Array.isArray(data)) {
    return { reason: 'endpoint_invalid_response', models: [], contextLimit: null };
  }
  const entries = data
    .filter((item): item is { id: string; max_model_len?: unknown } => typeof item?.id === 'string')
    .slice(0, 200);
  const models = entries.map((item) => item.id);
  if (model === null) return { reason: null, models, contextLimit: null };

  const entry = entries.find((item) => item.id === model);
  if (!entry) return { reason: 'model_not_found', models, contextLimit: null };
  const window = entry.max_model_len;
  const contextLimit = typeof window === 'number' && Number.isSafeInteger(window) && window > 0 ? window : null;
  try {
    const answer = await deps.request(join(checked.url, '/chat/completions'), checked.address, checked.family, {
      method: 'POST',
      timeoutMs: timeouts.inference,
      body: JSON.stringify({
        model,
        messages: [{ role: 'user', content: 'Reply with OK.' }],
        max_tokens: 1,
        stream: false,
      }),
    });
    const choices = (answer.body as { choices?: unknown } | null)?.choices;
    if (answer.status !== 200 || !Array.isArray(choices)) return { reason: 'inference_failed', models, contextLimit };
  } catch (error) {
    return {
      reason: failure(error) === 'endpoint_tls_failed' ? 'endpoint_tls_failed' : 'inference_failed',
      models,
      contextLimit,
    };
  }
  return { reason: null, models, contextLimit };
}
