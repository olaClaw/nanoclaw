import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  MODEL_SETTINGS_FILE,
  applyOpenCodeOverrides,
  readModelSettings,
  writeModelSettingsRaw,
} from './model-settings.js';

let dataDir: string;
const settings = (changes: Record<string, unknown> = {}) =>
  JSON.stringify({
    version: 1,
    provider: 'opencode',
    backend: 'openai',
    endpoint: 'http://LLM_HOST:8000/v1',
    model: 'openai/fixture-model-b',
    context_limit: 32768,
    applied_at: '2026-01-15T12:00:00.000Z',
    ...changes,
  });

beforeEach(() => {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-model-settings-'));
});
afterEach(() => fs.rmSync(dataDir, { recursive: true, force: true }));

describe('dashboard model settings', () => {
  it('leave the .env values alone when the panel never chose', () => {
    const env = { OPENCODE_MODEL: 'openai/old', OPENCODE_BASE_URL: 'http://OLD_HOST/v1' };
    applyOpenCodeOverrides(env, dataDir);
    expect(env).toEqual({ OPENCODE_MODEL: 'openai/old', OPENCODE_BASE_URL: 'http://OLD_HOST/v1' });
  });

  it('replace endpoint, model and small model, and fit the limits to the reported window', () => {
    writeModelSettingsRaw(settings(), dataDir);
    expect(fs.statSync(path.join(dataDir, MODEL_SETTINGS_FILE)).mode & 0o777).toBe(0o600);
    const env: Record<string, string> = {
      OPENCODE_PROVIDER: 'anthropic',
      OPENCODE_MODEL: 'openai/old',
      OPENCODE_SMALL_MODEL: 'openai/old-small',
      OPENCODE_BASE_URL: 'http://OLD_HOST/v1',
      OPENCODE_MODEL_CONTEXT_LIMIT: '131072',
      OPENCODE_MODEL_OUTPUT_LIMIT: '65536',
      OPENCODE_MODEL_INPUT_MODALITIES: 'image',
    };
    applyOpenCodeOverrides(env, dataDir);
    expect(env).toEqual({
      OPENCODE_PROVIDER: 'openai',
      OPENCODE_MODEL: 'openai/fixture-model-b',
      OPENCODE_SMALL_MODEL: 'openai/fixture-model-b',
      OPENCODE_BASE_URL: 'http://LLM_HOST:8000/v1',
      OPENCODE_MODEL_CONTEXT_LIMIT: '32768',
      OPENCODE_MODEL_INPUT_MODALITIES: 'image',
    });
    const fits: Record<string, string> = { OPENCODE_MODEL_OUTPUT_LIMIT: '4096' };
    applyOpenCodeOverrides(fits, dataDir);
    expect(fits.OPENCODE_MODEL_OUTPUT_LIMIT).toBe('4096');
  });

  it('ignore a file with an unexpected shape and follow rewrites', () => {
    writeModelSettingsRaw(settings({ model: 'fixture-model-b' }), dataDir);
    expect(readModelSettings(dataDir)).toBeNull();
    writeModelSettingsRaw(settings({ context_limit: null }), dataDir);
    expect(readModelSettings(dataDir)).toMatchObject({ model: 'openai/fixture-model-b', context_limit: null });
    writeModelSettingsRaw(null, dataDir);
    expect(readModelSettings(dataDir)).toBeNull();
  });
});
