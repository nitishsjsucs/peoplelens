// Startup warm-up (SPEC.md 25.4 D and risk 20). Workers Free allows 10 ms of CPU per
// request, and `npm run bench:cpu` measured a cold isolate's first /api/ask at 12 to 14 ms:
// most of that is V8 compiling, and building inline caches for, code that the first
// request is the first to run (the request screen, time-phrase and value regexes, the
// planner output schema, grounding, policy validation, catalog binding, BigQuery
// parameters and decoding, suppression, rounding and the narrative). Startup has its own,
// much larger limit (module evaluation measured about 31 ms), so one pure pass over that
// path runs when the app is built, on fixed questions and a synthetic result.
//
// It does no I/O, writes no log line, touches no Durable Object and uses no randomness or
// clock; its results are discarded. It runs once per catalog per isolate.
import { narrate } from "./answer/narrative";
import { bindQuery } from "./catalog/bind";
import { type CatalogFile, catalogKey, lookupEntry } from "./catalog/catalog";
import { applyComplementRule, complementProbes } from "./guard/complement";
import { rowUpperBound } from "./guard/cost";
import { roundForDisclosure } from "./guard/disclosure";
import { suppressCells, toCells } from "./guard/suppression";
import { ground } from "./planner/grounding";
import { isShortMetricReply, namesMetric, resolveDraft } from "./planner/planner";
import { buildMessages } from "./planner/prompt";
import { parsePlannerOutput } from "./planner/schema";
import { grainHint, resolveTimePhrase } from "./planner/timephrases";
import { findBreakdowns, findMetricCues, findValueMentions, screenRequest } from "./planner/values";
import { POLICY } from "./policy/policy";
import type { Principal } from "./policy/types";
import { DIMENSION_VALUES, validateIntent } from "./policy/validate";
import { decodeValue, toQueryParameters } from "./warehouse/bigquery";
import type { CellValue } from "./warehouse/types";

const PRINCIPAL: Principal = {
  subject: "warmup",
  kind: "user",
  role: "hr_business_partner",
  scope: { departments: ["Engineering", "Sales"] },
};

/** Two shapes of question: a grouped, filtered headcount, and a monthly compensation series. */
const QUESTIONS: { text: string; raw: Record<string, unknown> }[] = [
  {
    text: "Headcount by location for Sales in London and Austin at the end of last quarter",
    raw: { action: "query", metric: "headcount", group_by: ["location"], grain: "none", start: "", end: "",
      filters: [{ dimension: "department", values: ["Sales"] }, { dimension: "location", values: ["London", "Austin"] }] },
  },
  {
    text: "Average salary by job level in Sales, monthly in 2025",
    raw: { action: "query", metric: "compensation_summary", group_by: ["job_level"], grain: "month", start: "2025-01-01",
      end: "2025-12-31", filters: [] },
  },
];

const warmed = new WeakSet<object>();

export function warmUp(catalog: CatalogFile): void {
  if (warmed.has(catalog)) return;
  warmed.add(catalog);
  warmUpPasses(catalog);
}

/** Runs the pass for every warm-up question; returns how many reached the narrative
 * (tests pin that every one does, so a policy change cannot quietly empty the warm-up). */
export function warmUpPasses(catalog: CatalogFile): number {
  let done = 0;
  for (const q of QUESTIONS) {
    try {
      if (warmOne(catalog, q.text, q.raw)) done += 1;
    } catch {
      // A warm-up failure must never stop the Worker from starting; requests run the same code.
    }
  }
  return done;
}

function warmOne(catalog: CatalogFile, text: string, raw: Record<string, unknown>): boolean {
  const policy = POLICY;
  if (screenRequest(text)) return false;
  const hint = resolveTimePhrase(text);
  grainHint(text);
  const values = findValueMentions(text, policy);
  findBreakdowns(text);
  findMetricCues(text);
  namesMetric(text, policy);
  isShortMetricReply(text, policy);
  buildMessages({ question: text, principal: PRINCIPAL, timeHint: hint, values, previous: null }, policy);
  const output = parsePlannerOutput(JSON.stringify({ clarification_question: "", clarification_options: [], refusal_reason: "", ...raw }));
  if (!output || output.action !== "query") return false;
  const draft = ground(output.intent, { question: text, previous: null, followUp: false, pending: null, policy });
  const intent = resolveDraft(draft, hint, text, null, policy);
  const validation = validateIntent(intent, PRINCIPAL, policy, DIMENSION_VALUES);
  if (!validation.ok) return false;
  const v = validation.value;
  const entry = lookupEntry(catalog, catalogKey(v));
  if (!entry) return false;
  rowUpperBound(entry, v, DIMENSION_VALUES);
  const dialect = catalog.header.dialect;
  const queries = [v, ...complementProbes(v).map((p) => p.validated)].map((x) => bindQuery(entry, x, dialect, "warmup", policy));
  if (dialect === "bigquery") for (const q of queries) toQueryParameters(q.params);

  // A synthetic result in the catalog's column layout: one row per value of each breakdown.
  const cols = entry.columns;
  const dimCols = entry.group_by.map((d) => cols.dimensions[d] ?? d);
  const firstValues = entry.group_by.map((d) => DIMENSION_VALUES[d].slice(0, 3));
  const measureCols = [...new Set([...cols.values, cols.cohort])];
  const rows: CellValue[][] = [];
  const combos = firstValues.reduce<string[][]>((acc, vals) => acc.flatMap((a) => vals.map((x) => [...a, x])), [[]]);
  combos.forEach((dims, i) => {
    const period = cols.time ? [decodeValue("TIMESTAMP", String(1735689600 + i * 2678400) + ".0")] : [];
    const measures = measureCols.map((m) => (m === cols.cohort ? 5 + i * 7 : Number(decodeValue("FLOAT", String(150000.5 + i)))));
    rows.push([...dims, ...period, ...measures]);
  });
  const result = { columns: [...dimCols, ...(cols.time ? [cols.time] : []), ...measureCols], rows };
  const cells = toCells(result, cols as { dimensions: Record<string, string>; time: string | null; values: string[]; cohort: string }, entry.group_by);
  const { suppressKeys } = applyComplementRule(cells, [cells.map((c) => ({ ...c, cohort: c.cohort + 3 }))], { k: policy.limits.min_cohort });
  const suppressed = suppressCells(cells, { dims: entry.group_by, k: policy.limits.min_cohort, forced: suppressKeys });
  const metric = policy.metrics[v.metric];
  const shown = roundForDisclosure(suppressed.rows, metric);
  JSON.stringify({ rows: shown, narrative: narrate(metric, v.intent, shown, suppressed.counts, policy) });
  return true;
}
