/**
 * One read-only GET through an HTTPS-intercepting credential gateway, the way
 * an agent's own request would go: CONNECT through the agent's proxy URL,
 * TLS to the target trusting only the gateway's CA, the gateway injecting the
 * agent's credential. Used to check, before a provider switch, that every
 * agent group really has a working credential. Only the status and a small
 * parsed JSON body come back; nothing is logged.
 */
import http from 'http';
import https from 'https';
import tls from 'tls';

export interface ProxyProbeInput {
  /** The agent's proxy URL as the gateway hands it out (may carry credentials). */
  proxyUrl: string;
  /** Where this process reaches that proxy, when the agent-side name does not resolve here. */
  proxyHost?: string;
  /** PEM of the gateway's interception CA; the only trusted root. */
  ca: string;
  url: string;
  headers?: Record<string, string>;
  timeoutMs?: number;
}

export interface ProxyProbeResult {
  /** Upstream HTTP status; the proxy's own status when it refused the tunnel. */
  status: number;
  /** Where the answer came from: the target, or the proxy refusing CONNECT. */
  from: 'upstream' | 'proxy';
  /** Parsed JSON body when small and valid, else null. */
  body: unknown;
}

const MAX_BODY = 256 * 1024;

export function probeThroughProxy(input: ProxyProbeInput): Promise<ProxyProbeResult> {
  const target = new URL(input.url);
  if (target.protocol !== 'https:' || target.username || target.password) {
    return Promise.reject(new Error('probe target must be a plain https URL'));
  }
  const proxy = new URL(input.proxyUrl);
  const port = Number(target.port || 443);
  const timeoutMs = input.timeoutMs ?? 15_000;
  const auth =
    proxy.username || proxy.password
      ? `Basic ${Buffer.from(`${decodeURIComponent(proxy.username)}:${decodeURIComponent(proxy.password)}`).toString('base64')}`
      : undefined;
  return new Promise((resolve, reject) => {
    const deadline = setTimeout(() => {
      connect.destroy(new Error('timeout'));
      reject(new Error('timeout'));
    }, timeoutMs);
    deadline.unref();
    const connect = http.request({
      host: input.proxyHost || proxy.hostname,
      port: Number(proxy.port || 80),
      method: 'CONNECT',
      path: `${target.hostname}:${port}`,
      headers: { host: `${target.hostname}:${port}`, ...(auth ? { 'proxy-authorization': auth } : {}) },
      agent: false,
    });
    connect.on('error', (error) => {
      clearTimeout(deadline);
      reject(error);
    });
    connect.on('connect', (response, socket) => {
      if (response.statusCode !== 200) {
        clearTimeout(deadline);
        socket.destroy();
        resolve({ status: response.statusCode ?? 0, from: 'proxy', body: null });
        return;
      }
      const request = https.request(
        {
          host: target.hostname,
          port,
          method: 'GET',
          path: `${target.pathname}${target.search}`,
          headers: { accept: 'application/json', 'user-agent': 'nanoclaw-provider-check', ...input.headers },
          // No agent: Node then uses createConnection and never resolves the target itself.
          createConnection: () =>
            tls.connect({ socket, servername: target.hostname, ca: [input.ca], rejectUnauthorized: true }),
        },
        (answer) => {
          const chunks: Buffer[] = [];
          let size = 0;
          answer.on('data', (chunk: Buffer) => {
            size += chunk.length;
            if (size <= MAX_BODY) chunks.push(chunk);
          });
          answer.on('end', () => {
            clearTimeout(deadline);
            let body: unknown = null;
            if (size <= MAX_BODY) {
              try {
                body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
              } catch {
                body = null;
              }
            }
            socket.destroy();
            resolve({ status: answer.statusCode ?? 0, from: 'upstream', body });
          });
          answer.on('error', (error) => {
            clearTimeout(deadline);
            reject(error);
          });
        },
      );
      request.on('error', (error) => {
        clearTimeout(deadline);
        reject(error);
      });
      request.end();
    });
    connect.end();
  });
}
