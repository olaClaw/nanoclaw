import net from 'node:net';
import { fileURLToPath } from 'node:url';

const gatewayHost = 'onecli';
const gatewayPort = 10255;
const listenPort = 10255;

function connect(host, port) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host, port });
    socket.setTimeout(2000, () => socket.destroy(new Error('timeout')));
    socket.once('connect', () => {
      socket.setTimeout(0);
      resolve(socket);
    });
    socket.once('error', reject);
  });
}

export function createGatewayProxy({ targetHost = gatewayHost, targetPort = gatewayPort } = {}) {
  return net.createServer((client) => {
    void connect(targetHost, targetPort)
      .then((upstream) => {
        client.on('error', () => upstream.destroy());
        upstream.on('error', () => client.destroy());
        client.on('close', () => upstream.destroy());
        upstream.on('close', () => client.destroy());
        client.pipe(upstream);
        upstream.pipe(client);
      })
      .catch(() => client.destroy());
  });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  if (process.argv[2] === '--health') {
    connect(gatewayHost, gatewayPort)
      .then((socket) => socket.destroy())
      .catch(() => {
        process.exitCode = 1;
      });
  } else {
    createGatewayProxy().listen(listenPort, '0.0.0.0');
  }
}
