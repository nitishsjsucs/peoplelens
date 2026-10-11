// In-memory UserPort with the same semantics as UserDO, for fast pipeline tests.
import type { UserPort } from "../../src/ask";
import type { BudgetDecision, RateDecision, SessionState, TurnRecord } from "../../src/session/userDO";

export class MemoryUser implements UserPort {
  rate: number[] = [];
  used = new Map<string, number>();
  sessions = new Map<string, SessionState>();
  turns: TurnRecord[] = [];

  async reserveQuestion(now: number, limit = 30): Promise<RateDecision> {
    this.rate = this.rate.filter((t) => t > now - 60_000);
    if (this.rate.length >= limit) return { ok: false, count: this.rate.length, retryAfterMs: 1 };
    this.rate.push(now);
    return { ok: true, count: this.rate.length, retryAfterMs: 0 };
  }
  async reserveBytes(day: string, bytes: number, cap: number): Promise<BudgetDecision> {
    const used = this.used.get(day) ?? 0;
    if (used + bytes > cap) return { ok: false, used, cap };
    this.used.set(day, used + bytes);
    return { ok: true, used: used + bytes, cap };
  }
  async settleBytes(day: string, reserved: number, billed: number | null): Promise<void> {
    if (billed === null) return;
    this.used.set(day, Math.max(0, (this.used.get(day) ?? 0) - reserved + billed));
  }
  async getSession(id: string): Promise<SessionState | null> {
    const s = this.sessions.get(id);
    return s ? structuredClone(s) : null;
  }
  async saveTurn(id: string, turn: TurnRecord): Promise<boolean> {
    const s = this.sessions.get(id);
    if (!s) return false;
    s.turns += 1;
    if (turn.type === "answer" && turn.intent) s.lastIntent = turn.intent;
    s.pending = turn.pending;
    this.turns.push(turn);
    return true;
  }
  async createSession(now: number): Promise<string> {
    const id = `s${this.sessions.size + 1}`;
    this.sessions.set(id, { id, created: now, lastIntent: null, pending: null, turns: 0 });
    return id;
  }
}
