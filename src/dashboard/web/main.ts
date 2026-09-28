/**
 * Entry point of the dashboard container (D2).
 *
 *   NANOCLAW_DASHBOARD_STATE_DIR     private state directory (0700)
 *   NANOCLAW_DASHBOARD_ADMIN_SOCKET  the host boundary's Unix socket
 *   NANOCLAW_DASHBOARD_ORIGIN        exact https origin the browser uses
 *   NANOCLAW_DASHBOARD_INSECURE_HTTP `true` to allow an http origin (no certificate; unencrypted)
 *   NANOCLAW_DASHBOARD_PORT          listening port on the internal network (default 8080)
 *   NANOCLAW_DASHBOARD_OPS_SOCKET    optional: the root-side operations service's socket
 *
 * TLS is terminated by the reverse proxy in front of it and this port is
 * published on loopback only, except with the plain-HTTP opt-in, where the
 * browser reaches it directly on one LAN/VPN address, unencrypted.
 */
import { createDashboardServer, socketForward } from './server.js';
import { DashboardState } from './state.js';

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

export function dashboardConfigFromEnv(env: NodeJS.ProcessEnv = process.env) {
  const origin = env.NANOCLAW_DASHBOARD_ORIGIN ?? '';
  const insecureHttp = env.NANOCLAW_DASHBOARD_INSECURE_HTTP === 'true';
  const parsed = URL.canParse(origin) ? new URL(origin) : null;
  if (!parsed || parsed.origin !== origin || parsed.protocol !== (insecureHttp ? 'http:' : 'https:')) {
    throw new Error(
      insecureHttp
        ? 'NANOCLAW_DASHBOARD_INSECURE_HTTP needs an http origin without path, e.g. http://192.0.2.10:18082'
        : 'NANOCLAW_DASHBOARD_ORIGIN must be an https origin without path, e.g. https://panel.example.invalid',
    );
  }
  const port = Number(env.NANOCLAW_DASHBOARD_PORT ?? '8080');
  if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error('NANOCLAW_DASHBOARD_PORT is invalid');
  return {
    stateDir: env.NANOCLAW_DASHBOARD_STATE_DIR ?? required('NANOCLAW_DASHBOARD_STATE_DIR'),
    socket: env.NANOCLAW_DASHBOARD_ADMIN_SOCKET ?? required('NANOCLAW_DASHBOARD_ADMIN_SOCKET'),
    origin,
    insecureHttp,
    port,
    opsSocket: env.NANOCLAW_DASHBOARD_OPS_SOCKET || null,
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const config = dashboardConfigFromEnv();
  const state = new DashboardState(config.stateDir);
  const server = createDashboardServer({
    state,
    origin: config.origin,
    insecureHttp: config.insecureHttp,
    forward: socketForward(config.socket),
    opsForward: config.opsSocket ? socketForward(config.opsSocket) : undefined,
  });
  server.requestTimeout = 30_000;
  server.headersTimeout = 15_000;
  server.listen(config.port, () =>
    process.stdout.write(
      config.insecureHttp ? 'dashboard listening (plain HTTP: unencrypted)\n' : 'dashboard listening\n',
    ),
  );
  const stop = (): void => {
    server.close(() => process.exit(0));
  };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
}
