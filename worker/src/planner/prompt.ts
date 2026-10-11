// Planner prompt. Layout keeps a role-independent prefix (rules, catalog,
// few-shot examples from the dev set) so a local llama-server can reuse its KV
// cache across requests; everything specific to the request goes in the final
// user message. The prompt never contains query results.
//
// Workers Free allows 10 ms of CPU per request. Rebuilding the system prompt and
// the 12 few-shot messages cost 1.2 to 1.8 ms on every warm request and 5.5 to 8.6
// ms on a cold first call (SPEC.md 25.5), so the prefix is built once per isolate,
// at module scope (startup), and only the request message is built per request.
import fewshotJson from "../generated/fewshot.json";
import { POLICY, roleDimensions } from "../policy/policy";
import { DIMENSION_IDS, METRIC_IDS, type Policy, type Principal, type QueryIntent, type RoleId } from "../policy/types";
import type { ChatMessage } from "../llm/types";
import { AS_OF, type TimeHint, resolveTimePhrase } from "./timephrases";
import { type ValueMention, findValueMentions } from "./values";

interface FewShot {
  id: string;
  role?: RoleId;
  question: string;
  previous?: QueryIntent | null;
  output: Record<string, unknown>;
}

export const FEWSHOT = fewshotJson as unknown as FewShot[];

function catalogText(policy: Policy): string {
  const metrics = METRIC_IDS.map((id) => {
    const m = policy.metrics[id];
    return `- ${id}: ${m.definition} (also called: ${m.aliases.slice(0, 5).join(", ")})`;
  }).join("\n");
  const dims = DIMENSION_IDS.map((id) => {
    const d = policy.dimensions[id];
    return `- ${id}: ${d.values.join(" | ")}`;
  }).join("\n");
  return `Metrics:\n${metrics}\n\nDimensions and their exact values:\n${dims}`;
}

export function systemPrompt(policy: Policy = POLICY): string {
  return [
    "You turn workforce analytics questions about Lumen Works into a JSON query plan. You never write SQL and never see data.",
    `Today is ${AS_OF}. Data covers 2023-01-01 to ${AS_OF}.`,
    catalogText(policy),
    [
      "Rules:",
      '- action "query": set metric, group_by (0 to 2 dimensions the user asked to break down by), grain (none, month, quarter, year; use none unless the user asks for a trend or per-period values), start and end (YYYY-MM-DD), and filters (dimension plus exact values the question restricts to).',
      "- Headcount, fte and compensation_summary are point-in-time: with no period given use 2025-12-01 to 2025-12-31. Turnover, offer acceptance and time to fill are rates over a period: with no period given use 2025-01-01 to 2025-12-31.",
      "- If a resolved time range is given, use it for start and end.",
      '- action "clarify": the question is about the workforce but the metric is unclear. Ask one short question and give 2 to 4 options, each naming a metric.',
      '- action "refuse": set refusal_reason. WRITE_REQUEST for changing data; PII_REQUEST for names, emails, ids or person-level rows; DIMENSION_NOT_APPROVED for gender, age, manager or other unlisted attributes; METRIC_NOT_ALLOWED for a metric the role cannot use; OUT_OF_DOMAIN for anything that is not a workforce metric question.',
      "- Follow-ups such as \"now by location\" or \"what about 2024?\" change the previous query and keep the rest of it.",
      "- Leave unused string fields empty and unused lists empty.",
    ].join("\n"),
  ].join("\n\n");
}

function fewshotMessages(policy: Policy): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (const ex of FEWSHOT) {
    const role = ex.role ?? "people_analytics";
    const principal: Principal = {
      subject: "example",
      kind: "user",
      role,
      scope: { departments: role === "hr_business_partner" ? ["Engineering", "Sales"] : "all" },
    };
    const ctx: PromptContext = {
      question: ex.question,
      principal,
      timeHint: resolveTimePhrase(ex.question),
      values: findValueMentions(ex.question, policy),
      previous: ex.previous ?? null,
    };
    out.push({ role: "user", content: requestMessage(ctx, policy) });
    out.push({ role: "assistant", content: JSON.stringify(ex.output) });
  }
  return out;
}

export interface PromptContext {
  question: string;
  principal: Principal;
  timeHint: TimeHint | null;
  values: ValueMention[];
  previous: QueryIntent | null;
}

export function requestMessage(ctx: PromptContext, policy: Policy = POLICY): string {
  const role = policy.roles[ctx.principal.role];
  const lines: string[] = [];
  lines.push(
    `Role ${role.id}: metrics ${role.metrics.join(", ")}; grains ${role.grains.join(", ")}.`,
  );
  const dims = role.metrics.map((m) => `${m}: ${roleDimensions(policy, ctx.principal.role, m).join(", ") || "none"}`);
  lines.push(`Dimensions allowed per metric: ${dims.join("; ")}.`);
  if (ctx.principal.scope.departments !== "all") {
    lines.push(`This user may only see departments: ${ctx.principal.scope.departments.join(", ")}.`);
  }
  if (ctx.timeHint) lines.push(`Resolved time range for "${ctx.timeHint.phrase}": ${ctx.timeHint.start} to ${ctx.timeHint.end}.`);
  if (ctx.values.length > 0) {
    lines.push(`Values mentioned: ${ctx.values.map((v) => `${v.dimension}=${v.value}`).join(", ")}.`);
  }
  if (ctx.previous) {
    const p = ctx.previous;
    lines.push(
      `Previous query: ${JSON.stringify({ metric: p.metric, group_by: p.groupBy, grain: p.grain, start: p.timeRange.start, end: p.timeRange.end, filters: p.filters })}`,
    );
  }
  lines.push(`Question: ${ctx.question}`);
  return lines.join("\n");
}

let prefixBuilds = 0;
const prefixes = new WeakMap<Policy, readonly ChatMessage[]>();

/** How many times a prefix has been built in this isolate (tests pin it to once per policy). */
export function prefixBuildCount(): number {
  return prefixBuilds;
}

/** The role-independent prefix (system prompt plus few-shot messages), built once per policy object. */
export function plannerPrefix(policy: Policy = POLICY): readonly ChatMessage[] {
  let prefix = prefixes.get(policy);
  if (!prefix) {
    prefixBuilds += 1;
    const messages: ChatMessage[] = [{ role: "system", content: systemPrompt(policy) }, ...fewshotMessages(policy)];
    prefix = Object.freeze(messages.map((m) => Object.freeze(m)));
    prefixes.set(policy, prefix);
  }
  return prefix;
}

/** Built at module scope, so the work happens at isolate startup rather than in the first request. */
export const PLANNER_PREFIX = plannerPrefix(POLICY);

export function buildMessages(ctx: PromptContext, policy: Policy = POLICY): ChatMessage[] {
  return [...plannerPrefix(policy), { role: "user", content: requestMessage(ctx, policy) }];
}
