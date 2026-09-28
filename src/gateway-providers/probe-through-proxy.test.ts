import { execFileSync } from 'child_process';
import fs from 'fs';
import http from 'http';
import https from 'https';
import net from 'net';
import os from 'os';
import path from 'path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import { probeThroughProxy } from './probe-through-proxy.js';

const HOST = 'api.provider.test';
let work: string;
let ca: string;
let otherCa: string;
let upstream: https.Server;
let proxy: http.Server;
let proxyPort: number;
const seen: Array<{ path: string; header: string | undefined }> = [];
const tunnels = new Set<net.Socket>();

/** The agent's proxy URL with its credentials (built, so no literal looks like an address). */
function agentProxy(host: string, password = 'token-1'): string {
  const url = new URL(`http://${host}:${proxyPort}`);
  url.username = 'agent';
  url.password = password;
  return url.toString();
}

function openssl(...args: string[]): void {
  execFileSync('openssl', args, { cwd: work, stdio: 'ignore' });
}

beforeAll(async () => {
  work = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-probe-'));
  // A gateway CA, a leaf for the provider host signed by it, and an unrelated CA.
  openssl(
    'req',
    '-x509',
    '-newkey',
    'rsa:2048',
    '-nodes',
    '-keyout',
    'ca.key',
    '-out',
    'ca.pem',
    '-days',
    '2',
    '-subj',
    '/CN=fixture gateway CA',
  );
  openssl('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'leaf.key', '-out', 'leaf.csr', '-subj', `/CN=${HOST}`);
  fs.writeFileSync(path.join(work, 'san.cnf'), `subjectAltName=DNS:${HOST}\n`);
  openssl(
    'x509',
    '-req',
    '-in',
    'leaf.csr',
    '-CA',
    'ca.pem',
    '-CAkey',
    'ca.key',
    '-CAcreateserial',
    '-out',
    'leaf.pem',
    '-days',
    '2',
    '-extfile',
    'san.cnf',
  );
  openssl(
    'req',
    '-x509',
    '-newkey',
    'rsa:2048',
    '-nodes',
    '-keyout',
    'other.key',
    '-out',
    'other.pem',
    '-days',
    '2',
    '-subj',
    '/CN=unrelated CA',
  );
  ca = fs.readFileSync(path.join(work, 'ca.pem'), 'utf8');
  otherCa = fs.readFileSync(path.join(work, 'other.pem'), 'utf8');

  upstream = https.createServer(
    { key: fs.readFileSync(path.join(work, 'leaf.key')), cert: fs.readFileSync(path.join(work, 'leaf.pem')) },
    (request, response) => {
      seen.push({ path: request.url ?? '', header: request.headers['anthropic-version'] as string | undefined });
      const authorized = request.headers.authorization === 'Bearer injected-by-gateway';
      response.writeHead(authorized ? 200 : 401, { 'content-type': 'application/json' });
      response.end(JSON.stringify(authorized ? { data: [{ id: 'model-a' }] } : { error: 'unauthorized' }));
    },
  );
  await new Promise<void>((resolve) => upstream.listen(0, '127.0.0.1', resolve));
  const upstreamPort = (upstream.address() as net.AddressInfo).port;

  // The gateway: checks the agent's proxy token, tunnels to the provider, "injects" its credential
  // (here the upstream fixture simply trusts a header the test sets).
  proxy = http.createServer();
  proxy.on('connect', (request, client: net.Socket) => {
    tunnels.add(client);
    const expected = `Basic ${Buffer.from('agent:token-1').toString('base64')}`;
    if (request.headers['proxy-authorization'] !== expected || request.url !== `${HOST}:443`) {
      client.end('HTTP/1.1 407 Proxy Authentication Required\r\n\r\n');
      return;
    }
    const server = net.connect(upstreamPort, '127.0.0.1', () => {
      tunnels.add(server);
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      server.pipe(client);
      client.pipe(server);
    });
    server.on('error', () => client.destroy());
  });
  await new Promise<void>((resolve) => proxy.listen(0, '127.0.0.1', resolve));
  proxyPort = (proxy.address() as net.AddressInfo).port;
}, 60_000);

afterAll(async () => {
  for (const socket of tunnels) socket.destroy();
  proxy.closeAllConnections();
  upstream.closeAllConnections();
  await new Promise<void>((resolve) => proxy.close(() => resolve()));
  await new Promise<void>((resolve) => upstream.close(() => resolve()));
  fs.rmSync(work, { recursive: true, force: true });
});

describe('probe through the credential gateway', () => {
  it('tunnels as the agent, trusts only the gateway CA and returns the status and JSON', async () => {
    const result = await probeThroughProxy({
      proxyUrl: agentProxy('host.docker.internal'),
      proxyHost: '127.0.0.1',
      ca,
      url: `https://${HOST}/v1/models`,
      headers: { authorization: 'Bearer injected-by-gateway', 'anthropic-version': '2023-06-01' },
    });
    expect(result).toEqual({ status: 200, from: 'upstream', body: { data: [{ id: 'model-a' }] } });
    expect(seen.at(-1)).toEqual({ path: '/v1/models', header: '2023-06-01' });

    const unauthorized = await probeThroughProxy({
      proxyUrl: agentProxy('127.0.0.1'),
      ca,
      url: `https://${HOST}/v1/models`,
    });
    expect(unauthorized).toMatchObject({ status: 401, from: 'upstream' });
  });

  it('reports a refused tunnel as the gateway speaking, and never trusts another CA', async () => {
    const refused = await probeThroughProxy({
      proxyUrl: agentProxy('127.0.0.1', 'wrong'),
      ca,
      url: `https://${HOST}/v1/models`,
    });
    expect(refused).toEqual({ status: 407, from: 'proxy', body: null });
    await expect(
      probeThroughProxy({
        proxyUrl: agentProxy('127.0.0.1'),
        ca: otherCa,
        url: `https://${HOST}/v1/models`,
      }),
    ).rejects.toThrow();
    await expect(
      probeThroughProxy({ proxyUrl: `http://127.0.0.1:${proxyPort}`, ca, url: `http://${HOST}/v1/models` }),
    ).rejects.toThrow('plain https');
  });
});
