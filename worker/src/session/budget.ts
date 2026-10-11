// Names shared by AccountBudgetDO and its callers, kept apart from the Durable
// Object class so the request path does not import `cloudflare:workers` at runtime.
export const ACCOUNT_BUDGET_NAME = "peoplelens";
export type BudgetCounter = "neurons" | "audit_queue_messages" | "audit_direct_puts";
export const BUDGET_COUNTERS: readonly BudgetCounter[] = ["neurons", "audit_queue_messages", "audit_direct_puts"];

/** The UTC day (YYYY-MM-DD) that the account's daily allowances reset on. */
export function utcDay(now: number): string {
  return new Date(now).toISOString().slice(0, 10);
}
