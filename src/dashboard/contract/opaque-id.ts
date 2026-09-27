/**
 * Public identifiers for the dashboard API.
 *
 * Internal IDs are not guaranteed to be meaningless: installs migrated from v1
 * or imported from another host may carry IDs built from folder names, chat
 * IDs or platform handles. The API therefore never exposes them. Each public
 * ID is an HMAC of the internal ID under a per-install key that lives only in
 * the host's private state, so the same row keeps the same public ID across
 * requests and restarts, and a public ID reveals nothing without the key.
 *
 * Resolving a public ID back scans the candidate internal IDs: resource
 * counts are small, and it keeps the mapping stateless.
 */
import { createHmac, timingSafeEqual } from 'crypto';

import { PUBLIC_ID_PREFIXES, type PublicIdKind } from './api.js';

/** At least 32 random bytes, generated once per install and never exported. */
export const MIN_KEY_BYTES = 32;

function checkKey(key: Buffer): void {
  if (key.length < MIN_KEY_BYTES) throw new Error('dashboard id key too short');
}

export function publicId(kind: PublicIdKind, internalId: string, key: Buffer): string {
  checkKey(key);
  const digest = createHmac('sha256', key).update(`${kind}\0${internalId}`).digest('hex').slice(0, 32);
  return `${PUBLIC_ID_PREFIXES[kind]}_${digest}`;
}

/** The internal ID whose public ID matches, or null. Constant-time per comparison. */
export function resolvePublicId(
  kind: PublicIdKind,
  candidate: string,
  internalIds: Iterable<string>,
  key: Buffer,
): string | null {
  const wanted = Buffer.from(candidate);
  let match: string | null = null;
  for (const internalId of internalIds) {
    const actual = Buffer.from(publicId(kind, internalId, key));
    if (actual.length === wanted.length && timingSafeEqual(actual, wanted)) match = internalId;
  }
  return match;
}
