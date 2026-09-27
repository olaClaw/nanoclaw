/**
 * Privacy canaries for dashboard tests.
 *
 * The synthetic install (./synthetic-install.ts) plants one canary in every
 * field the API must never return: platform IDs, chat and person names, thread
 * IDs, folders and mount paths, MCP URLs and tokens, message text. Tests
 * serialize HTTP responses, logs and audit records and require that no canary
 * appears. Values use reserved documentation ranges and domains only, so they
 * can never collide with real data or trip the privacy source gate.
 *
 * The generic detectors catch shapes a canary list cannot enumerate: any IPv4
 * address, email, URL or absolute filesystem path in an API response is a
 * leak, whatever its value.
 */

export const CANARIES = {
  platform_id: 'canary-platform-7d41',
  phone: '+15550100173',
  email: 'canary.person@example.invalid',
  person_name: 'Canary Person 5b2e',
  chat_name: 'Canary Chat Room 90af',
  thread_id: 'canary-thread-3c8d',
  folder: 'canary-folder-e1f0',
  mount_path: '/srv/canary-mount-4a6b/private',
  mcp_url: 'http://canary-mcp.example.invalid:18765/mcp',
  llm_endpoint: 'http://198.51.100.77:8000/v1',
  token: 'canary-token-2f9a61c0d4e8b7a3',
  message_text: 'canary message text 8e12',
  image_tag: 'canary-registry.example.invalid/agent:canary-6c3f',
  internal_agent_id: 'ag-canary-internal-51d9',
  internal_session_id: 'sess-canary-internal-a07e',
  internal_messaging_group_id: 'mg-canary-internal-0b64',
} as const;

export type CanaryName = keyof typeof CANARIES;

const DETECTORS: ReadonlyArray<readonly [string, RegExp]> = [
  ['ipv4', /(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])/],
  ['email', /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/],
  ['url', /\b[a-z][a-z0-9+.-]*:\/\/[^\s"']+/i],
  ['absolute_path', /(?:^|["\s=:])\/(?:srv|home|root|app|var|etc|tmp|Users|workspace|data)\//],
];

/**
 * Names (never values) of every canary and detector that matches `text`.
 * An empty result means the text is clean.
 */
export function findLeaks(text: string): string[] {
  const found: string[] = [];
  for (const [name, value] of Object.entries(CANARIES)) {
    if (text.includes(value)) found.push(`canary:${name}`);
  }
  for (const [name, pattern] of DETECTORS) {
    if (pattern.test(text)) found.push(`shape:${name}`);
  }
  return found;
}
