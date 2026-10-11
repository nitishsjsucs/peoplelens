// One SQLite-backed Durable Object per user: sessions, pending clarifications,
// the daily byte budget and the per-minute rate limit. Filled in by the cost
// guard and session commits; the class exists from the start so the binding
// and its new_sqlite_classes migration are stable.
import { DurableObject } from "cloudflare:workers";
import type { Env } from "../env";

export class UserDO extends DurableObject<Env> {
  async ping(): Promise<string> {
    return "pong";
  }
}
