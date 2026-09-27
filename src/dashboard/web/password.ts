/**
 * Administrator password hashing (D2).
 *
 * scrypt from Node's crypto: memory-hard, built in, no native dependency.
 * The operator chose it over Argon2id, which Node 22 does not ship. The
 * stored format is versioned (`scrypt$1$N$r$p$salt$hash`), so a later move to
 * Argon2id can rehash at the next successful login.
 *
 * Only the hash is ever stored. Verification compares in constant time and
 * runs the same work for an unknown or malformed record, so timing does not
 * reveal whether a password is configured.
 */
import { randomBytes, scrypt as scryptCallback, timingSafeEqual, type ScryptOptions } from 'crypto';

export interface ScryptParams {
  N: number;
  r: number;
  p: number;
}

/** 128 MiB of memory per hash; about a quarter of a second on a small server. */
export const DEFAULT_PARAMS: ScryptParams = { N: 2 ** 17, r: 8, p: 1 };
const KEY_BYTES = 32;
const SALT_BYTES = 16;
export const MIN_PASSWORD_LENGTH = 12;
export const MAX_PASSWORD_LENGTH = 1024;

function derive(password: string, salt: Buffer, params: ScryptParams): Promise<Buffer> {
  const options: ScryptOptions = {
    N: params.N,
    r: params.r,
    p: params.p,
    maxmem: 256 * params.N * params.r + 1024 * 1024,
  };
  return new Promise((resolve, reject) => {
    scryptCallback(password.normalize('NFKC'), salt, KEY_BYTES, options, (error, key) =>
      error ? reject(error) : resolve(key),
    );
  });
}

/** Refuse passwords that are too short or too long; no other composition rules. */
export function passwordProblem(password: string): 'too_short' | 'too_long' | null {
  const length = [...password.normalize('NFKC')].length;
  if (length < MIN_PASSWORD_LENGTH) return 'too_short';
  if (length > MAX_PASSWORD_LENGTH) return 'too_long';
  return null;
}

export async function hashPassword(password: string, params: ScryptParams = DEFAULT_PARAMS): Promise<string> {
  const problem = passwordProblem(password);
  if (problem) throw new Error(`password ${problem}`);
  const salt = randomBytes(SALT_BYTES);
  const key = await derive(password, salt, params);
  return ['scrypt', '1', params.N, params.r, params.p, salt.toString('base64url'), key.toString('base64url')].join('$');
}

interface Parsed {
  params: ScryptParams;
  salt: Buffer;
  key: Buffer;
}

const FORMAT = /^scrypt\$1\$(\d{1,8})\$(\d{1,3})\$(\d{1,3})\$([A-Za-z0-9_-]{16,64})\$([A-Za-z0-9_-]{43})$/;

function parse(stored: string): Parsed | null {
  const match = FORMAT.exec(stored);
  if (!match) return null;
  const [N, r, p] = [Number(match[1]), Number(match[2]), Number(match[3])];
  if (N < 2 ** 14 || N > 2 ** 20 || (N & (N - 1)) !== 0 || r < 1 || r > 32 || p < 1 || p > 16) return null;
  return { params: { N, r, p }, salt: Buffer.from(match[4], 'base64url'), key: Buffer.from(match[5], 'base64url') };
}

/** A fixed record used when none is stored, so failures take the same time. */
const DUMMY: Parsed = { params: DEFAULT_PARAMS, salt: Buffer.alloc(SALT_BYTES), key: Buffer.alloc(KEY_BYTES) };

export async function verifyPassword(password: string, stored: string | null): Promise<boolean> {
  const parsed = (stored && parse(stored)) || null;
  const record = parsed ?? DUMMY;
  const candidate = await derive(password.slice(0, MAX_PASSWORD_LENGTH * 4), record.salt, record.params);
  return timingSafeEqual(candidate, record.key) && parsed !== null;
}

/** True when a stored hash uses weaker parameters than the current default. */
export function needsRehash(stored: string, params: ScryptParams = DEFAULT_PARAMS): boolean {
  const parsed = parse(stored);
  return !parsed || parsed.params.N < params.N || parsed.params.r < params.r || parsed.params.p < params.p;
}

export function isPasswordRecord(stored: string): boolean {
  return parse(stored) !== null;
}
