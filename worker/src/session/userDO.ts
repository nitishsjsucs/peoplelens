// One SQLite-backed Durable Object per user (keyed by the principal subject):
// sessions and pending clarifications, the daily byte budget and the per-minute
// rate limit. No question text and no result values are stored.
import { DurableObject } from "cloudflare:workers";
import type { Env } from "../env";
import type { QueryIntent } from "../policy/types";

export interface RateDecision {
  ok: boolean;
  count: number;
  retryAfterMs: number;
}

export interface BudgetDecision {
  ok: boolean;
  used: number;
  cap: number;
}

export interface PendingOption {
  id: string;
  label: string;
  intent: QueryIntent | null;
}

export interface PendingClarification {
  question: string;
  options: PendingOption[];
  base: QueryIntent | null;
}

export interface TurnRecord {
  ts: number;
  type: "answer" | "clarification" | "refusal";
  intent: QueryIntent | null;
  pending: PendingClarification | null;
  code: string | null;
  catalogKey: string | null;
}

export interface SessionState {
  id: string;
  created: number;
  lastIntent: QueryIntent | null;
  pending: PendingClarification | null;
  turns: number;
}

export const RATE_WINDOW_MS = 60_000;

export class UserDO extends DurableObject<Env> {
  private readonly sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`CREATE TABLE IF NOT EXISTS rate_events (ts INTEGER NOT NULL)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, used INTEGER NOT NULL)`);
    this.sql.exec(
      `CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, created INTEGER NOT NULL,
        last_intent TEXT, pending TEXT, turns INTEGER NOT NULL DEFAULT 0)`,
    );
    this.sql.exec(
      `CREATE TABLE IF NOT EXISTS turns (session_id TEXT NOT NULL, n INTEGER NOT NULL, record TEXT NOT NULL,
        PRIMARY KEY (session_id, n))`,
    );
  }

  async reserveQuestion(now: number, limit = 30): Promise<RateDecision> {
    this.sql.exec(`DELETE FROM rate_events WHERE ts <= ?`, now - RATE_WINDOW_MS);
    const rows = this.sql.exec<{ n: number; oldest: number | null }>(
      `SELECT COUNT(*) AS n, MIN(ts) AS oldest FROM rate_events`,
    ).one();
    if (rows.n >= limit) {
      return { ok: false, count: rows.n, retryAfterMs: Math.max(0, (rows.oldest ?? now) + RATE_WINDOW_MS - now) };
    }
    this.sql.exec(`INSERT INTO rate_events (ts) VALUES (?)`, now);
    return { ok: true, count: rows.n + 1, retryAfterMs: 0 };
  }

  async reserveBytes(day: string, bytes: number, cap: number): Promise<BudgetDecision> {
    const row = this.sql.exec<{ used: number }>(`SELECT used FROM budget WHERE day = ?`, day).toArray()[0];
    const used = row?.used ?? 0;
    if (bytes < 0 || used + bytes > cap) return { ok: false, used, cap };
    this.sql.exec(
      `INSERT INTO budget (day, used) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET used = used + excluded.used`,
      day,
      bytes,
    );
    return { ok: true, used: used + bytes, cap };
  }

  /** Replace a reservation with the bytes actually billed (null keeps the reservation). */
  async settleBytes(day: string, reserved: number, billed: number | null): Promise<void> {
    if (billed === null) return;
    this.sql.exec(`UPDATE budget SET used = MAX(0, used - ? + ?) WHERE day = ?`, reserved, billed, day);
  }

  async budgetUsed(day: string): Promise<number> {
    const row = this.sql.exec<{ used: number }>(`SELECT used FROM budget WHERE day = ?`, day).toArray()[0];
    return row?.used ?? 0;
  }

  async createSession(now: number): Promise<string> {
    const id = crypto.randomUUID();
    this.sql.exec(`INSERT INTO sessions (id, created, turns) VALUES (?, ?, 0)`, id, now);
    return id;
  }

  async getSession(id: string): Promise<SessionState | null> {
    const row = this.sql
      .exec<{ id: string; created: number; last_intent: string | null; pending: string | null; turns: number }>(
        `SELECT id, created, last_intent, pending, turns FROM sessions WHERE id = ?`,
        id,
      )
      .toArray()[0];
    if (!row) return null;
    return {
      id: row.id,
      created: row.created,
      lastIntent: row.last_intent ? (JSON.parse(row.last_intent) as QueryIntent) : null,
      pending: row.pending ? (JSON.parse(row.pending) as PendingClarification) : null,
      turns: row.turns,
    };
  }

  /** Returns false for an unknown session (errors are values across RPC). */
  async saveTurn(id: string, turn: TurnRecord): Promise<boolean> {
    const session = await this.getSession(id);
    if (!session) return false;
    const n = session.turns + 1;
    const lastIntent = turn.type === "answer" && turn.intent ? JSON.stringify(turn.intent) : (session.lastIntent ? JSON.stringify(session.lastIntent) : null);
    this.sql.exec(
      `UPDATE sessions SET last_intent = ?, pending = ?, turns = ? WHERE id = ?`,
      lastIntent,
      turn.pending ? JSON.stringify(turn.pending) : null,
      n,
      id,
    );
    this.sql.exec(`INSERT INTO turns (session_id, n, record) VALUES (?, ?, ?)`, id, n, JSON.stringify(turn));
    return true;
  }
}
