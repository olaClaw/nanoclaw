import net from 'node:net';
import path from 'node:path';

// The CLI socket is bound after the host has initialized its runtime and DB.
// Connecting detects a stale socket more reliably than checking its existence.
const socketPath = path.join(process.cwd(), 'data', 'ncl.sock');
const socket = net.createConnection(socketPath);
const timeout = setTimeout(() => socket.destroy(new Error('timeout')), 2000);

socket.once('connect', () => {
  clearTimeout(timeout);
  socket.destroy();
});
socket.once('error', () => {
  clearTimeout(timeout);
  process.exitCode = 1;
});
