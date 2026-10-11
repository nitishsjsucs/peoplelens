// Deterministic reading of a question: mentioned dimension values, breakdown
// dimensions, and screening for requests the system refuses whatever the LLM
// says (writes, person-level data, sensitive or unapproved attributes, access
// escalation). Screening runs before the LLM call.
import { POLICY } from "../policy/policy";
import { DIMENSION_IDS, type DimensionId, type MetricId, type Policy, type RefusalCode } from "../policy/types";

export interface ValueMention {
  dimension: DimensionId;
  value: string;
}

function norm(text: string): string {
  return ` ${text.toLowerCase().replace(/[’']/g, "'").replace(/[^a-z0-9<>+&'\- ]+/g, " ").replace(/\s+/g, " ")} `;
}

function escape(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function has(t: string, phrase: string): boolean {
  return new RegExp(`(?<![a-z0-9])${escape(phrase)}(?![a-z0-9])`).test(t);
}

// Words that are also everyday English and must not be read as a value on their own.
const AMBIGUOUS_VALUES = new Set(["people"]);

/** Dimension values mentioned in the question (by value or alias), in policy order. */
export function findValueMentions(text: string, policy: Policy = POLICY): ValueMention[] {
  // "full-time equivalent" names the FTE metric, not the full_time employment type.
  const t = norm(text).replace(/full[- ]time equivalents?/g, "fte");
  const out: ValueMention[] = [];
  for (const dim of DIMENSION_IDS) {
    const def = policy.dimensions[dim];
    for (const value of def.values) {
      const lower = value.toLowerCase();
      const phrases = [lower, ...Object.entries(def.aliases).filter(([, v]) => v === value).map(([a]) => a)].filter(
        (p) => !AMBIGUOUS_VALUES.has(p),
      );
      if (dim === "employment_type") phrases.push(lower.replace("_", " "), lower.replace("_", "-"));
      if (phrases.some((p) => has(t, p))) out.push({ dimension: dim, value });
    }
  }
  // "People team" style mentions of the People department
  if (/ people (team|department|org|function) /.test(t) && !out.some((m) => m.value === "People")) {
    out.push({ dimension: "department", value: "People" });
  }
  // A level range such as "L4 and above" or "senior (L5+)" is left to the planner.
  return out;
}

const DIMENSION_WORDS: [DimensionId, RegExp][] = [
  ["department", /\b(departments?|teams?|orgs?|organizations?|functions?|business units?|divisions?)\b/],
  ["location", /\b(locations?|offices?|sites?|cities|city|geograph(y|ies)|regions?|countries|country|hubs?)\b/],
  ["job_level", /\b(job levels?|levels?|grades?|seniority|career levels?)\b/],
  ["employment_type", /\b(employment types?|full[- ]time (vs|versus|and|or) part[- ]time|part[- ]time (vs|versus|and|or) full[- ]time|worker types?|contract types?)\b/],
  ["tenure_band", /\b(tenure|tenure bands?|length of service|years of service)\b/],
];

/** Dimensions the question asks to break down by ("by department", "per office", "across levels"). */
export function findBreakdowns(text: string): DimensionId[] {
  const t = text.toLowerCase();
  const out: DimensionId[] = [];
  const cue = /\b(by|per|across|for each|for every|each|every|split by|broken down by|breakdown by|breakdown of|grouped by|group by|compare|comparison of|by\s+\w+\s+and)\b/;
  for (const [dim, re] of DIMENSION_WORDS) {
    const m = t.match(re);
    if (!m || m.index === undefined) continue;
    const before = t.slice(Math.max(0, m.index - 28), m.index);
    if (cue.test(before) || /\b(breakdown|split|mix|distribution)\b/.test(t.slice(m.index))) out.push(dim);
  }
  return out;
}

export interface Screen {
  code: RefusalCode;
  message: string;
}

const WRITE_PATTERNS: RegExp[] = [
  /\b(drop|truncate|alter)\b[^.?]{0,30}\b(tables?|views?|schemas?|datasets?|databases?)\b/,
  /\bdelete\s+(from|all|every|the|this|that|these|those|employee|employees|record|records|row|rows|data|everyone)\b/,
  /\binsert\s+(into|a|new)\b/,
  /\bupdate\s+(the\s+)?(table|record|records|row|rows|salary|salaries|data|employee|employees|database)\b/,
  /\bupdate\b[^?]*\bset\b/,
  /\b(change|modify|edit|overwrite|set|raise|increase|cut|lower|reduce|bump)\s+(the\s+|my\s+|his\s+|her\s+|their\s+|our\s+|all\s+)?(salary|salaries|pay|compensation|title|titles|level|levels|records?)\b/,
  /\b(give|grant|award)\b[^.?]{0,40}\b(raise|raises|promotion|promotions|bonus|bonuses)\b/,
  /\bpromote\s+(everyone|all|him|her|them|the|every|\w+\s+in)\b/,
  /\b(fire|terminate|lay off|layoff)\s+(\w+\s+)?(employee|employees|people|staff|him|her|them|everyone|someone)\b/,
  /\b(create|add|make)\s+(a\s+)?(new\s+)?(table|view|employee record|record|column)\b/,
  /;\s*(drop|delete|update|insert|alter|create|merge|grant)\b/,
];

const ESCALATION_PATTERNS: RegExp[] = [
  /\b(grant|give|make)\s+(me|myself|us)\b[^.?]*\b(access|role|admin|administrator|permission|permissions|people analytics|executive|superuser)\b/,
  /\b(switch|change|elevate|escalate|upgrade)\s+(my\s+)?(role|access|permissions?)\b/,
  /\b(act|acting|pretend|treat me)\s+(as|like)\s+(an?\s+)?(admin|administrator|executive|people analytics|superuser|root)\b/,
  /\b(bypass|ignore|disable|turn off|override)\s+(the\s+)?(suppression|privacy|policy|policies|restrictions?|scope|limits?|guardrails?|threshold)\b/,
];

const PII_PATTERNS: RegExp[] = [
  /\b(names?|full names?|first names?|last names?)\s+(of|for)\b/,
  /\blist\s+(out\s+)?(all\s+|the\s+|every\s+|each\s+)?(names|employees|people|staff|individuals|candidates|everyone|workers)\b/,
  /\b(show|give|send|export|dump|print)\s+(me\s+)?(all|every|each)\s+(employee|employees|person|people|staff|individuals?|candidates?|records?|rows?)\b(?!\s+(count|counts|number|total))/,
  /\bwho\s+(are|is|were|was|left|joined|quit|resigned|got|earns|makes|has|have|accepted|declined)\b/,
  /\b(e-?mails?|email address(es)?|phone numbers?|home address(es)?|ssn|social security)\b/,
  /\b(employee|candidate)[ _]?(ids?|numbers?)\b/,
  /\b(date of birth|dates of birth|birthdays?|birth dates?|dob)\b/,
  /\b(per|by|for each|each|every|individual)\s+(employee|person|candidate|individual|worker)\b/,
  /\b(row[- ]level|record[- ]level|person[- ]level|employee[- ]level|individual[- ]level|raw (data|rows|records))\b/,
  /\bby name\b/,
];

const UNAPPROVED_PATTERNS: [RegExp, string][] = [
  [/\b(gender|genders|sex|male|female|men|women|man|woman|non-?binary)\b/, "gender"],
  [/\b(age|ages|aged|age bands?|age groups?|older|younger|millennials?|boomers?|gen z)\b/, "age"],
  [/\b(race|races|ethnicity|ethnicities|ethnic|religion|religious|disability|disabilities|veterans?|marital|pregnan\w*|sexual orientation|nationality)\b/, "protected attributes"],
  [/\b(by|per|for each|each)\s+manager\b|\bmanagers?\s+(with|who)\b|\breports? to\b|\bteam leads?\b/, "manager"],
  [/\b(job famil(y|ies)|role famil(y|ies))\b/, "job family"],
];

/** Requests refused before planning, whatever the LLM would do. */
export function screenRequest(text: string): Screen | null {
  const t = text.toLowerCase().replace(/[’']/g, "'");
  if (WRITE_PATTERNS.some((re) => re.test(t))) {
    return { code: "WRITE_REQUEST", message: "PeopleLens is read-only: it cannot change, add or delete any data." };
  }
  if (ESCALATION_PATTERNS.some((re) => re.test(t))) {
    return { code: "SCOPE_VIOLATION", message: "Access is set by your role binding and cannot be changed from a question." };
  }
  if (PII_PATTERNS.some((re) => re.test(t))) {
    return { code: "PII_REQUEST", message: "PeopleLens answers with aggregates only; it never returns names, contact details, identifiers or person-level rows." };
  }
  for (const [re, label] of UNAPPROVED_PATTERNS) {
    if (re.test(t)) {
      return {
        code: "DIMENSION_NOT_APPROVED",
        message: `Analysis by ${label} is not an approved dimension. Approved: department, location, job level, employment type, tenure band.`,
      };
    }
  }
  return null;
}

const METRIC_CUES: [MetricId, RegExp][] = [
  ["offer_acceptance_rate", /\b(offer acceptance|acceptance rate|offers? accepted|accept(ed)? (rate|offers?)|offer accept|offer (rate|conversion)|candidates? accept|accept\w*\b[^.?]{0,30}\boffers?\b|offers?\b[^.?]{0,30}\baccept)/],
  ["time_to_fill_days", /\b(time[- ]to[- ]fill|days to fill|time to hire|days to hire|fill (time|rate|speed)|how (long|fast|quickly)\b[^?]*\b(fill|hire)|how long (does it take|it takes) to (fill|hire))/],
  ["turnover_rate", /\b(turnover|attrition|churn|leavers?|separations?|quit rate|exits?|people leaving|left the company)\b/],
  ["compensation_summary", /\b(compensation|salary|salaries|pay|paid|base pay|earn|earnings|wages?)\b/],
  ["fte", /\b(fte|ftes|full[- ]time equivalents?|full time equivalents?)\b/],
  ["headcount", /\b(headcount|head count|employees|people|staff|workforce|how many|employee count|number of (employees|people|staff)|team size|size of)\b/],
];

/** Metrics a question points at, strongest cue first. */
export function findMetricCues(text: string): MetricId[] {
  const t = text.toLowerCase();
  return METRIC_CUES.filter(([, re]) => re.test(t)).map(([m]) => m);
}
