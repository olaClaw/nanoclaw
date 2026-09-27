import http from 'http';
import net from 'net';
import { afterEach, describe, expect, it } from 'vitest';

import { checkEndpoint, pinnedRequest, probeEndpoint, type EndpointDeps, type ProbeRequest } from './model-endpoint.js';

function allowed(): net.BlockList {
  const list = new net.BlockList();
  list.addSubnet('100.64.0.0', 10, 'ipv4');
  list.addSubnet('fc00::', 7, 'ipv6');
  return list;
}

function deps(changes: Partial<EndpointDeps> = {}): EndpointDeps {
  const own = new net.BlockList();
  own.addSubnet('100.99.0.0', 16, 'ipv4');
  return {
    lookup: async () => [{ address: '100.64.1.2', family: 4 }],
    own,
    allowed: allowed(),
    request: async () => ({ status: 500, body: null }),
    ...changes,
  };
}

/** An OpenAI-compatible fake: lists two models and answers completions. */
function fakeServer(overrides: Partial<Record<'models' | 'chat', { status: number; body: unknown }>> = {}) {
  const calls: Array<{ path: string; address: string; body?: string }> = [];
  const request: ProbeRequest = async (target, address, _family, init) => {
    calls.push({ path: target.pathname, address, body: init.body });
    if (target.pathname.endsWith('/models')) {
      return (
        overrides.models ?? {
          status: 200,
          body: { data: [{ id: 'fixture-model-a', max_model_len: 32768 }, { id: 'fixture/model-b' }, { id: 7 }] },
        }
      );
    }
    return overrides.chat ?? { status: 200, body: { choices: [{ message: { content: 'OK' } }] } };
  };
  return { calls, request };
}

describe('endpoint address rules', () => {
  it('accepts private LAN/VPN addresses and names that resolve only to them', async () => {
    for (const raw of [
      'http://100.64.1.2:8000/v1',
      'https://[fd7a:115c:a1e0::5]/v1',
      'http://llm.example.test:8000/v1',
    ]) {
      const checked = await checkEndpoint(raw, deps());
      expect(checked.ok, raw).toBe(true);
    }
  });

  it('refuses public, loopback, link-local, own-network and service-name targets', async () => {
    const cases: Array<[string, string]> = [
      ['http://198.51.100.77:8000/v1', 'endpoint_not_private'],
      ['http://127.0.0.1:8000/v1', 'endpoint_not_private'],
      ['http://169.254.169.254/latest', 'endpoint_not_private'],
      ['http://[::1]:8000/v1', 'endpoint_not_private'],
      ['http://100.99.0.4:5432/', 'endpoint_not_private'],
      ['http://postgres:5432/', 'endpoint_not_private'],
      ['http://localhost:8000/v1', 'endpoint_not_private'],
      ['http://[::ffff:127.0.0.1]:8000/v1', 'endpoint_not_private'],
      ['ftp://100.64.1.2/v1', 'endpoint_invalid'],
      ['http://user:secret@100.64.1.2/v1', 'endpoint_invalid'],
      ['http://100.64.1.2/v1?next=http://198.51.100.1', 'endpoint_invalid'],
      ['http://100.64.1.2/v1#fragment', 'endpoint_invalid'],
      ['not a url', 'endpoint_invalid'],
    ];
    for (const [raw, reason] of cases) {
      expect(await checkEndpoint(raw, deps()), raw).toEqual({ ok: false, reason });
    }
  });

  it('refuses a name when any resolved address is outside the LAN, or when it does not resolve', async () => {
    const mixed = deps({
      lookup: async () => [
        { address: '100.64.1.2', family: 4 },
        { address: '198.51.100.9', family: 4 },
      ],
    });
    expect(await checkEndpoint('http://llm.example.test/v1', mixed)).toEqual({
      ok: false,
      reason: 'endpoint_not_private',
    });
    const missing = deps({
      lookup: async () => {
        throw new Error('ENOTFOUND');
      },
    });
    expect(await checkEndpoint('http://llm.example.test/v1', missing)).toEqual({
      ok: false,
      reason: 'endpoint_unresolvable',
    });
  });
});

describe('endpoint probe', () => {
  it('lists models, checks the one asked for with a one-token completion, and reads its window', async () => {
    const server = fakeServer();
    const result = await probeEndpoint(
      'http://llm.example.test:8000/v1/',
      'fixture-model-a',
      deps({ request: server.request }),
    );
    expect(result).toEqual({
      reason: null,
      models: ['fixture-model-a', 'fixture/model-b'],
      contextLimit: 32768,
    });
    expect(server.calls.map((call) => [call.path, call.address])).toEqual([
      ['/v1/models', '100.64.1.2'],
      ['/v1/chat/completions', '100.64.1.2'],
    ]);
    expect(JSON.parse(server.calls[1].body!)).toMatchObject({ model: 'fixture-model-a', max_tokens: 1 });
  });

  it('reports a stable code for every way the endpoint can fail', async () => {
    const cases: Array<[Parameters<typeof fakeServer>[0], string | null, string]> = [
      [{ models: { status: 401, body: null } }, 'fixture-model-a', 'endpoint_unauthorized'],
      [{ models: { status: 200, body: { unexpected: true } } }, 'fixture-model-a', 'endpoint_invalid_response'],
      [{ models: { status: 302, body: null } }, 'fixture-model-a', 'endpoint_invalid_response'],
      [{}, 'missing-model', 'model_not_found'],
      [{ chat: { status: 500, body: null } }, 'fixture-model-a', 'inference_failed'],
    ];
    for (const [overrides, model, reason] of cases) {
      const server = fakeServer(overrides);
      const result = await probeEndpoint('http://100.64.1.2/v1', model, deps({ request: server.request }));
      expect(result.reason, reason).toBe(reason);
    }
    const down = await probeEndpoint(
      'http://100.64.1.2/v1',
      null,
      deps({
        request: async () => {
          throw Object.assign(new Error('connect'), { code: 'ECONNREFUSED' });
        },
      }),
    );
    expect(down.reason).toBe('endpoint_unreachable');
    // A refused address never reaches the network.
    const server = fakeServer();
    const refused = await probeEndpoint(
      'http://198.51.100.77/v1',
      'fixture-model-a',
      deps({ request: server.request }),
    );
    expect([refused.reason, server.calls.length]).toEqual(['endpoint_not_private', 0]);
  });
});

describe('pinned request', () => {
  let server: http.Server | null = null;
  afterEach(async () => {
    await new Promise<void>((resolve) => (server ? server.close(() => resolve()) : resolve()));
    server = null;
  });

  async function listen(handler: http.RequestListener): Promise<number> {
    server = http.createServer(handler);
    await new Promise<void>((resolve) => server!.listen(0, '127.0.0.1', resolve));
    return (server.address() as net.AddressInfo).port;
  }

  it('connects to the checked address under the original host name and follows no redirect', async () => {
    const seen: string[] = [];
    const port = await listen((request, response) => {
      seen.push(`${request.headers.host} ${request.headers.authorization ?? '-'}`);
      response.writeHead(302, { location: 'http://198.51.100.1/' }).end();
    });
    const target = new URL(`http://llm.example.invalid:${port}/v1/models`);
    const answer = await pinnedRequest(target, '127.0.0.1', 4, { method: 'GET', timeoutMs: 2_000 });
    expect(answer.status).toBe(302);
    expect(seen).toEqual([`llm.example.invalid:${port} -`]);
  });

  it('gives up on oversized bodies and slow answers', async () => {
    const port = await listen((request, response) => {
      if (request.url === '/big') response.end('x'.repeat(300 * 1024));
      // /slow never answers.
    });
    const big = new URL(`http://127.0.0.1:${port}/big`);
    await expect(pinnedRequest(big, '127.0.0.1', 4, { method: 'GET', timeoutMs: 2_000 })).rejects.toThrow();
    const slow = new URL(`http://127.0.0.1:${port}/slow`);
    await expect(pinnedRequest(slow, '127.0.0.1', 4, { method: 'GET', timeoutMs: 200 })).rejects.toThrow('timeout');
  });
});
