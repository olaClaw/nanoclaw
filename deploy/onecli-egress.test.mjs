import assert from 'node:assert/strict';
import net from 'node:net';
import { test } from 'node:test';

import { createGatewayProxy } from './onecli-egress.mjs';

function listen(server) {
  return new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
}

function close(server) {
  return new Promise((resolve) => server.close(resolve));
}

test('egress proxy forwards bytes to its configured gateway only', async () => {
  const gateway = net.createServer((socket) => socket.pipe(socket));
  await listen(gateway);
  const targetPort = gateway.address().port;
  const proxy = createGatewayProxy({ targetHost: '127.0.0.1', targetPort });
  await listen(proxy);
  try {
    const response = await new Promise((resolve, reject) => {
      const client = net.createConnection({ host: '127.0.0.1', port: proxy.address().port });
      client.once('error', reject);
      client.once('data', (data) => {
        client.destroy();
        resolve(data.toString());
      });
      client.once('connect', () => client.write('probe'));
    });
    assert.equal(response, 'probe');
  } finally {
    await close(proxy);
    await close(gateway);
  }
});
