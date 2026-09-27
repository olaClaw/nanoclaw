/**
 * Plain-HTTP MCP servers on the internal agent network (Compose service names).
 * MCP_PLAIN_HTTP_HOSTS is read at module load, so each case reloads the modules.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

async function load(hosts: string | undefined) {
  if (hosts === undefined) delete process.env.NANOCLAW_MCP_PLAIN_HTTP_HOSTS;
  else process.env.NANOCLAW_MCP_PLAIN_HTTP_HOSTS = hosts;
  vi.resetModules();
  const config = await import('./config.js');
  const containerConfig = await import('./container-config.js');
  return { hosts: config.MCP_PLAIN_HTTP_HOSTS, parse: containerConfig.parseMcpServerConfig };
}

afterEach(() => {
  delete process.env.NANOCLAW_MCP_PLAIN_HTTP_HOSTS;
  vi.resetModules();
});

describe('plain-HTTP MCP hosts', () => {
  it('accepts plain HTTP only for configured single-label service hosts', async () => {
    const { hosts, parse } = await load(' infomaniak-mail,nextcloud-calendar ,mail.example.com,');
    expect(hosts).toEqual(['infomaniak-mail', 'nextcloud-calendar']);
    expect(
      parse({ type: 'http', url: 'http://infomaniak-mail:18765/mcp', headers: { Authorization: 'x' } }),
    ).toMatchObject({
      type: 'http',
      url: 'http://infomaniak-mail:18765/mcp',
    });
    expect(() => parse({ url: 'http://mail.example.com/mcp' })).toThrow(/HTTPS/);
    expect(() => parse({ url: 'http://other-service:18765/mcp' })).toThrow(/HTTPS/);
  });

  it('keeps the HTTPS rule for service names when nothing is configured', async () => {
    const { hosts, parse } = await load(undefined);
    expect(hosts).toEqual([]);
    expect(() => parse({ url: 'http://infomaniak-mail:18765/mcp' })).toThrow(/HTTPS/);
    expect(parse({ url: 'http://localhost:8080/mcp' })).toMatchObject({ type: 'http' });
  });
});
