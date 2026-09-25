import { describe, expect, it, vi } from 'vitest';

import {
  buildOpenCodeSmokeConfig,
  parseSmokeArguments,
  runLocalOpenAiSmoke,
  verifySmokeOutput,
  type SmokeDependencies,
} from './local-openai-smoke.js';

const marker = 'NANOCLAW_LOCAL_TOOL_OK';

function output(): string {
  return [
    JSON.stringify({ type: 'tool_use', part: { state: { output: marker } } }),
    JSON.stringify({ type: 'text', part: { text: marker } }),
  ].join('\n');
}

describe('local OpenAI container smoke test', () => {
  it('builds a keyless OpenAI-compatible configuration', () => {
    expect(JSON.parse(buildOpenCodeSmokeConfig('http://192.0.2.10:8000/v1', 'local-model'))).toMatchObject({
      enabled_providers: ['openai'],
      model: 'openai/local-model',
      provider: {
        openai: {
          npm: '@ai-sdk/openai-compatible',
          options: { apiKey: 'placeholder', baseURL: 'http://192.0.2.10:8000/v1' },
          models: { 'local-model': { tool_call: true } },
        },
      },
    });
  });

  it('runs an ephemeral Podman container and verifies tool use plus final reply', async () => {
    const runContainer = vi.fn(() => ({ status: 0, stdout: output(), stderr: '' }));
    const dependencies: SmokeDependencies = {
      readConfig: () => ({ OPENCODE_BASE_URL: 'http://192.0.2.10:8000/v1' }),
      discoverModels: async () => ['example-local-model'],
      defaultImage: () => 'nanoclaw-agent:test',
      runContainer,
    };

    const result = await runLocalOpenAiSmoke(parseSmokeArguments(['--runtime', 'podman']), dependencies);

    expect(result).toMatchObject({ runtime: 'podman', model: 'openai/example-local-model' });
    expect(runContainer).toHaveBeenCalledOnce();
    const args = runContainer.mock.calls[0][1];
    expect(args).toContain('--rm');
    expect(args).toContain('nanoclaw-agent:test');
    expect(args).toContain('openai/example-local-model');
  });

  it('rejects incomplete output and ambiguous model catalogs', async () => {
    expect(() => verifySmokeOutput(JSON.stringify({ type: 'text', part: { text: marker } }))).toThrow(
      'tool result and final reply',
    );
    const dependencies: SmokeDependencies = {
      readConfig: () => ({ OPENCODE_BASE_URL: 'http://192.0.2.10:8000/v1' }),
      discoverModels: async () => ['model-b', 'model-a'],
      defaultImage: () => 'unused',
      runContainer: vi.fn(),
    };
    await expect(runLocalOpenAiSmoke({ runtime: 'podman' }, dependencies)).rejects.toThrow(
      'Available: model-a, model-b',
    );
    expect(dependencies.runContainer).not.toHaveBeenCalled();
  });
});
