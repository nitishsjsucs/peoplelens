// Client for the Worker API. In production the browser carries the Cloudflare
// Access session cookie and Access adds Cf-Access-Jwt-Assertion at the edge.
// Locally (loopback only) a token minted by `uv run pl token --role <role>` can be
// pasted and is sent in that header, matching the Worker's local JWKS.

export interface Column {
  name: string;
  kind: "dimension" | "time" | "measure";
  unit?: string;
}

export type Row = Record<string, string | number | boolean | null>;

export interface AskResponse {
  request_id: string;
  type: "answer" | "clarification" | "refusal";
  answer?: {
    metric: string;
    intent: { metric: string; group_by: string[]; grain: string; time_range: { start: string; end: string } };
    definition: string;
    columns: Column[];
    rows: Row[];
    suppressed_cells: { primary: number; complementary: number; hidden_sum: number; complement: number };
    narrative: string;
  };
  clarification?: { question: string; options: { id: string; label: string }[] };
  refusal?: { code: string; message: string; alternatives: string[] };
  meta: { latency_ms: number; catalog_key: string | null; llm_provider: string; warehouse: string };
}

export interface Me {
  role: string;
  scope: { departments: string[] | "all" };
  metrics: string[];
  dimensions: string[];
  grains: string[];
}

export function isLoopback(hostname: string): boolean {
  return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]";
}

export function headers(localToken: string | null): Record<string, string> {
  const h: Record<string, string> = { "content-type": "application/json" };
  if (localToken) h["Cf-Access-Jwt-Assertion"] = localToken;
  return h;
}

export class ApiClient {
  constructor(
    private readonly token: string | null,
    private readonly fetcher: typeof fetch = (input, init) => fetch(input, init),
  ) {}

  private async call<T>(path: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method: body === undefined ? "GET" : "POST", headers: headers(this.token), credentials: "same-origin" };
    if (body !== undefined) init.body = JSON.stringify(body);
    const res = await this.fetcher(path, init);
    const json = (await res.json().catch(() => ({}))) as T & { refusal?: { message?: string } };
    if (!res.ok && res.status !== 200) {
      throw new Error(json.refusal?.message ?? `HTTP ${res.status}`);
    }
    return json;
  }

  me(): Promise<Me> {
    return this.call<Me>("/api/me");
  }

  async session(): Promise<string> {
    return (await this.call<{ session_id: string }>("/api/sessions", {})).session_id;
  }

  ask(sessionId: string, message: string): Promise<AskResponse> {
    return this.call<AskResponse>("/api/ask", { session_id: sessionId, message });
  }

  choose(sessionId: string, optionId: string): Promise<AskResponse> {
    return this.call<AskResponse>("/api/ask", { session_id: sessionId, choose_option: optionId });
  }
}
