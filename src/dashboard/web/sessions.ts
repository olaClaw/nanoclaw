/**
 * Server-side sessions and the login throttle (D2).
 *
 * Sessions live in memory: a restart logs the administrator out, which is
 * the safe direction. The cookie carries a random 256-bit token; only its
 * SHA-256 is kept, so a heap dump does not yield usable cookies. Each session
 * records the credential generation it was created under; a password change
 * or local reset bumps the generation and every older session stops working.
 *
 * The throttle is global (there is one account): after a few failures every
 * login waits, with a doubling lock persisted in the state directory. The
 * local `unlock` command clears it.
 */
import { createHash, randomBytes, timingSafeEqual } from 'crypto';

import type { DashboardState } from './state.js';

export const IDLE_MS = 30 * 60 * 1000;
export const ABSOLUTE_MS = 8 * 60 * 60 * 1000;
export const REAUTH_MS = 5 * 60 * 1000;
export const MAX_SESSIONS = 8;

const FREE_FAILURES = 5;
const FIRST_LOCK_MS = 60 * 1000;
const MAX_LOCK_MS = 60 * 60 * 1000;

export interface Session {
  createdAt: number;
  lastSeen: number;
  reauthUntil: number | null;
  csrf: string;
  generation: number;
}

export interface SessionView {
  expires_at: string;
  idle_expires_at: string;
  reauth_expires_at: string | null;
  csrf_token: string;
}

const digest = (token: string): string => createHash('sha256').update(token).digest('hex');
const iso = (ms: number): string => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

export class SessionStore {
  private readonly sessions = new Map<string, Session>();

  constructor(private readonly now: () => number = Date.now) {}

  /** A new session with a fresh token: login always rotates. */
  create(generation: number): { token: string; session: Session } {
    this.sweep(generation);
    if (this.sessions.size >= MAX_SESSIONS) {
      const oldest = [...this.sessions.entries()].sort((a, b) => a[1].lastSeen - b[1].lastSeen)[0];
      this.sessions.delete(oldest[0]);
    }
    const token = randomBytes(32).toString('base64url');
    const at = this.now();
    const session: Session = {
      createdAt: at,
      lastSeen: at,
      reauthUntil: null,
      csrf: randomBytes(32).toString('base64url'),
      generation,
    };
    this.sessions.set(digest(token), session);
    return { token, session };
  }

  /** The live session for a cookie token, refreshing its idle timer; null when expired or revoked. */
  get(token: string | undefined, generation: number, touch = true): Session | null {
    if (!token || !/^[A-Za-z0-9_-]{43}$/.test(token)) return null;
    const key = digest(token);
    const session = this.sessions.get(key);
    if (!session) return null;
    const at = this.now();
    if (session.generation !== generation || at - session.lastSeen > IDLE_MS || at - session.createdAt > ABSOLUTE_MS) {
      this.sessions.delete(key);
      return null;
    }
    if (touch) session.lastSeen = at;
    return session;
  }

  destroy(token: string | undefined): void {
    if (token) this.sessions.delete(digest(token));
  }

  markReauth(session: Session): void {
    session.reauthUntil = Math.min(this.now() + REAUTH_MS, session.createdAt + ABSOLUTE_MS);
  }

  hasReauth(session: Session): boolean {
    return session.reauthUntil !== null && this.now() <= session.reauthUntil;
  }

  csrfMatches(session: Session, header: string | undefined): boolean {
    if (!header) return false;
    const a = Buffer.from(session.csrf);
    const b = Buffer.from(header);
    return a.length === b.length && timingSafeEqual(a, b);
  }

  view(session: Session): SessionView {
    return {
      expires_at: iso(session.createdAt + ABSOLUTE_MS),
      idle_expires_at: iso(Math.min(session.lastSeen + IDLE_MS, session.createdAt + ABSOLUTE_MS)),
      reauth_expires_at: this.hasReauth(session) ? iso(session.reauthUntil!) : null,
      csrf_token: session.csrf,
    };
  }

  private sweep(generation: number): void {
    const at = this.now();
    for (const [key, session] of this.sessions) {
      if (
        session.generation !== generation ||
        at - session.lastSeen > IDLE_MS ||
        at - session.createdAt > ABSOLUTE_MS
      ) {
        this.sessions.delete(key);
      }
    }
  }
}

export class LoginThrottle {
  constructor(
    private readonly state: DashboardState,
    private readonly now: () => number = Date.now,
  ) {}

  /** Milliseconds to wait before another attempt is allowed; 0 when free. */
  waitMs(): number {
    const { locked_until } = this.state.throttle();
    if (!locked_until) return 0;
    return Math.max(0, Date.parse(locked_until) - this.now());
  }

  failure(): void {
    const record = this.state.throttle();
    const failures = record.failures + 1;
    const over = failures - FREE_FAILURES;
    const lock = over > 0 ? Math.min(FIRST_LOCK_MS * 2 ** (over - 1), MAX_LOCK_MS) : 0;
    this.state.setThrottle({ failures, locked_until: lock ? new Date(this.now() + lock).toISOString() : null });
  }

  success(): void {
    this.state.setThrottle({ failures: 0, locked_until: null });
  }
}
