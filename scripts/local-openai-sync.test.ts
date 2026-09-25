import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { parseSyncArguments, syncLocalOpenAi, type SyncDependencies } from './local-openai-sync.js';

const originalCwd = process.cwd();
let directory: string;

beforeEach(() => {
  directory = fs.mkdtempSync(path.join(os.tmpdir(), 'local-openai-sync-'));
  process.chdir(directory);
  for (const key of [
    'OPENCODE_PROVIDER',
    'OPENCODE_MODEL',
    'OPENCODE_SMALL_MODEL',
    'OPENCODE_BASE_URL',
    'OPENCODE_AUTH_MODE',
    'NANOCLAW_EGRESS_LOCKDOWN',
  ])
    vi.stubEnv(key, undefined);
});

afterEach(() => {
  process.chdir(originalCwd);
  fs.rmSync(directory, { recursive: true, force: true });
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe('local OpenAI model synchronization', () => {
  it('uses the real NanoClaw env reader/writer and OpenCode model discovery', async () => {
    fs.writeFileSync(
      '.env',
      'OPENCODE_PROVIDER=openai\nOPENCODE_BASE_URL=http://llm.example.invalid:8000/v1\nOPENCODE_MODEL=openai/old\nOPENCODE_SMALL_MODEL=openai/old\nOTHER=preserve\n',
    );
    const request = vi.fn(async () => new Response(JSON.stringify({ data: [{ id: 'new-model' }] })));
    vi.stubGlobal('fetch', request);

    const result = await syncLocalOpenAi({ groups: [] });

    expect(result).toMatchObject({ model: 'openai/new-model', changed: true });
    expect(request).toHaveBeenCalledWith(new URL('http://llm.example.invalid:8000/v1/models'), expect.any(Object));
    expect(fs.readFileSync('.env', 'utf8')).toContain('OPENCODE_MODEL=openai/new-model\n');
    expect(fs.readFileSync('.env', 'utf8')).toContain('OPENCODE_SMALL_MODEL=openai/new-model\n');
    expect(fs.readFileSync('.env', 'utf8')).toContain('OTHER=preserve\n');
  });

  it('requires an explicit choice for multiple models and changes nothing', async () => {
    const saveModels = vi.fn();
    const dependencies: SyncDependencies = {
      readConfig: () => ({
        OPENCODE_PROVIDER: 'openai',
        OPENCODE_BASE_URL: 'http://llm.example.invalid:8000/v1',
      }),
      discoverModels: async () => ['model-b', 'model-a'],
      saveModels,
      restartGroup: vi.fn(),
      environment: {},
    };

    await expect(syncLocalOpenAi({ groups: [] }, dependencies)).rejects.toThrow('Available: model-a, model-b');
    expect(saveModels).not.toHaveBeenCalled();
  });

  it('updates both defaults and restarts each requested group once', async () => {
    const saveModels = vi.fn();
    const restartGroup = vi.fn();
    const dependencies: SyncDependencies = {
      readConfig: () => ({
        OPENCODE_PROVIDER: 'openai',
        OPENCODE_BASE_URL: 'http://llm.example.invalid:8000/v1',
        OPENCODE_MODEL: 'openai/old',
        OPENCODE_SMALL_MODEL: 'openai/old',
      }),
      discoverModels: async () => ['model-b', 'model-a'],
      saveModels,
      restartGroup,
      environment: {},
    };

    const args = parseSyncArguments(['--model', 'openai/model-b', '--group', 'alpha', '--group', 'alpha']);
    const result = await syncLocalOpenAi(args, dependencies);

    expect(saveModels).toHaveBeenCalledWith('model-b');
    expect(restartGroup).toHaveBeenCalledTimes(1);
    expect(restartGroup).toHaveBeenCalledWith('alpha');
    expect(result.restartedGroups).toEqual(['alpha']);
  });

  it('refuses configurations that cannot route directly to the endpoint', async () => {
    const dependencies: SyncDependencies = {
      readConfig: () => ({
        OPENCODE_PROVIDER: 'openai',
        OPENCODE_BASE_URL: 'http://llm.example.invalid:8000/v1',
        NANOCLAW_EGRESS_LOCKDOWN: 'true',
      }),
      discoverModels: vi.fn(),
      saveModels: vi.fn(),
      restartGroup: vi.fn(),
      environment: {},
    };

    await expect(syncLocalOpenAi({ groups: [] }, dependencies)).rejects.toThrow('prevents direct access');
    expect(dependencies.discoverModels).not.toHaveBeenCalled();
  });
});
