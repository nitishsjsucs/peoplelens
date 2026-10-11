// This repo's share of the Cloudflare account's daily Free plan allowances
// (SPEC.md 25.4 B). The account is shared with three other repos, and on Workers
// Free the 10,000 Workers AI neurons per UTC day cannot be extended by paying, so
// PeopleLens enforces its own cap instead of assuming the whole allowance.
//
// One SQLite-backed instance, idFromName("peoplelens"). Counters:
//   neurons               cap free_plan.worker_neurons_per_day (6,000)
//   audit_queue_messages  cap free_plan.audit_queue_messages_per_day (400)
//   audit_direct_puts     cap free_plan.audit_direct_puts_per_day (2,000; R2 Class A beyond the queue share)
// A Durable Object runs one request at a time and none of these methods awaits
// between its read and its write, so concurrent reservations can never exceed a cap.
import { DurableObject } from "cloudflare:workers";
import type { Env } from "../env";
import { BUDGET_COUNTERS, type BudgetCounter, utcDay } from "./budget";
import type { BudgetDecision } from "./userDO";

export { ACCOUNT_BUDGET_NAME, BUDGET_COUNTERS, type BudgetCounter, utcDay } from "./budget";
/** Rows older than this many days are pruned on every reservation. */
export const KEEP_DAYS = 7;

const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** Inputs are checked and refused as values: an exception thrown inside a Durable
 * Object RPC method is reported by workerd as uncaught (see PROGRESS.md deviations). */
function valid(day: string, counter: string, amount: number): boolean {
  return DAY.test(day) && (BUDGET_COUNTERS as readonly string[]).includes(counter) && Number.isFinite(amount) && amount >= 0;
}

export class AccountBudgetDO extends DurableObject<Env> {
  private readonly sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(
      `CREATE TABLE IF NOT EXISTS usage (day TEXT NOT NULL, counter TEXT NOT NULL, used INTEGER NOT NULL,
        PRIMARY KEY (day, counter))`,
    );
  }

  private used(day: string, counter: string): number {
    const row = this.sql.exec<{ used: number }>(`SELECT used FROM usage WHERE day = ? AND counter = ?`, day, counter).toArray()[0];
    return row?.used ?? 0;
  }

  /** Reserve `amount` (rounded up) if it fits under `cap` for that day; otherwise change
   * nothing. Invalid input is refused (fails closed). */
  async reserve(day: string, counter: BudgetCounter, amount: number, cap: number): Promise<BudgetDecision> {
    if (!valid(day, counter, amount) || !Number.isFinite(cap)) return { ok: false, used: 0, cap };
    const want = Math.ceil(amount);
    const used = this.used(day, counter);
    if (used + want > cap) return { ok: false, used, cap };
    this.sql.exec(
      `INSERT INTO usage (day, counter, used) VALUES (?, ?, ?)
        ON CONFLICT(day, counter) DO UPDATE SET used = used + excluded.used`,
      day,
      counter,
      want,
    );
    const cutoff = utcDay(Date.parse(`${day}T00:00:00Z`) - KEEP_DAYS * 86_400_000);
    this.sql.exec(`DELETE FROM usage WHERE day < ?`, cutoff);
    return { ok: true, used: used + want, cap };
  }

  /**
   * Replace a reservation with what was actually used (rounded up). `actual` null
   * keeps the reservation (the conservative choice when usage is unknown).
   */
  async settle(day: string, counter: BudgetCounter, reserved: number, actual: number | null): Promise<void> {
    if (actual === null || !valid(day, counter, reserved) || !valid(day, counter, actual)) return;
    this.sql.exec(
      `UPDATE usage SET used = MAX(0, used - ? + ?) WHERE day = ? AND counter = ?`,
      Math.ceil(reserved),
      Math.ceil(actual),
      day,
      counter,
    );
  }

  async usage(day: string): Promise<Record<BudgetCounter, number>> {
    if (!DAY.test(day)) return { neurons: 0, audit_queue_messages: 0, audit_direct_puts: 0 };
    return {
      neurons: this.used(day, "neurons"),
      audit_queue_messages: this.used(day, "audit_queue_messages"),
      audit_direct_puts: this.used(day, "audit_direct_puts"),
    };
  }
}
