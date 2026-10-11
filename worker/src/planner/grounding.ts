// Grounding of a planner draft in the question text. The LLM chooses the action
// and the metric; breakdowns, filters and grain must be supported by the words
// of the question (or by the conversation it follows up on). Unsupported slots
// that a model invents are dropped, and slots the question states plainly are
// added. Deterministic and LLM-independent.
import { POLICY } from "../policy/policy";
import { DIMENSION_IDS, type DimensionId, type Grain, type Policy, type QueryIntent } from "../policy/types";
import type { DraftIntent } from "./schema";
import { grainHint } from "./timephrases";
import { type ValueMention, findBreakdowns, findValueMentions } from "./values";

const EXCLUSION = /\b(except(?: for)?|excluding|exclude|other than|apart from|besides|without|but not|not including|minus|outside(?: of)?)\s+(the\s+)?$/;
const COMPARISON = /\b(compare|comparing|comparison|vs\.?|versus|against|between|relative to|and)\b/;

function escape(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Values that the question explicitly excludes ("everyone except Legal"). */
export function findExclusions(question: string, policy: Policy = POLICY): ValueMention[] {
  const t = question.toLowerCase().replace(/[’']/g, "'");
  const out: ValueMention[] = [];
  for (const dim of DIMENSION_IDS) {
    const def = policy.dimensions[dim];
    for (const value of def.values) {
      const phrases = [value.toLowerCase(), ...Object.entries(def.aliases).filter(([, v]) => v === value).map(([a]) => a)];
      for (const p of phrases) {
        const re = new RegExp(`(?<![a-z0-9])${escape(p)}(?![a-z0-9])`, "g");
        let m: RegExpExecArray | null;
        while ((m = re.exec(t)) !== null) {
          const before = t.slice(Math.max(0, m.index - 30), m.index);
          if (EXCLUSION.test(before) && !out.some((o) => o.dimension === dim && o.value === value)) {
            out.push({ dimension: dim, value });
          }
        }
      }
    }
  }
  return out;
}

export interface GroundingContext {
  question: string;
  previous: QueryIntent | null;
  followUp: boolean;
  pending: Partial<DraftIntent> | null;
  policy?: Policy;
}

export function ground(draft: DraftIntent, ctx: GroundingContext): DraftIntent {
  const policy = ctx.policy ?? POLICY;
  const mentions = findValueMentions(ctx.question, policy);
  const exclusions = findExclusions(ctx.question, policy);
  const excluded = new Set(exclusions.map((e) => `${e.dimension}\u0000${e.value}`));
  const included = mentions.filter((m) => !excluded.has(`${m.dimension}\u0000${m.value}`));
  const breakdowns = findBreakdowns(ctx.question);

  // values per dimension stated in the question
  const stated = new Map<DimensionId, string[]>();
  for (const m of included) stated.set(m.dimension, [...(stated.get(m.dimension) ?? []), m.value]);
  const comparisonDims = [...stated.entries()]
    .filter(([, vals]) => vals.length >= 2 && COMPARISON.test(ctx.question.toLowerCase()))
    .map(([d]) => d);

  // group-by
  let groupBy: DimensionId[];
  const context = ctx.followUp ? ctx.previous?.groupBy ?? [] : (ctx.pending?.groupBy ?? []);
  if (breakdowns.length > 0) {
    groupBy = [...breakdowns];
    for (const d of draft.groupBy) if (comparisonDims.includes(d) && !groupBy.includes(d)) groupBy.push(d);
  } else {
    const grounded = new Set<DimensionId>([...comparisonDims, ...context]);
    groupBy = draft.groupBy.filter((d) => grounded.has(d));
    if (groupBy.length === 0 && (ctx.followUp || ctx.pending)) groupBy = [...context];
  }

  // filters
  const filters = new Map<DimensionId, string[]>();
  for (const [d, vals] of stated) filters.set(d, [...new Set(vals)]);
  for (const e of exclusions) {
    if (filters.has(e.dimension)) continue;
    const out = policy.dimensions[e.dimension].values.filter((v) => !excluded.has(`${e.dimension}\u0000${v}`));
    filters.set(e.dimension, out);
  }
  const contextFilters = ctx.followUp ? ctx.previous?.filters ?? [] : (ctx.pending?.filters ?? []);
  for (const f of contextFilters) {
    if (!filters.has(f.dimension) && mentions.every((m) => m.dimension !== f.dimension)) filters.set(f.dimension, [...f.values]);
  }

  // grain
  let grain: Grain = grainHint(ctx.question) ?? "none";
  if (grain === "none" && ctx.followUp && ctx.previous) grain = ctx.previous.grain;
  if (grain === "none" && ctx.pending?.grain) grain = ctx.pending.grain;

  return {
    ...draft,
    groupBy,
    grain,
    filters: [...filters.entries()].map(([dimension, values]) => ({ dimension, values })),
  };
}
