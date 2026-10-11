// Deterministic time-phrase resolution. The as-of date is fixed (2025-12-31):
// results never depend on the wall clock. The planner sees the resolved range
// as a hint, and the server applies it when present, so a small model cannot
// mis-compute calendar arithmetic.
import type { Grain } from "../policy/types";

export const AS_OF = "2025-12-31";
export const DATA_START = "2023-01-01";

export interface TimeHint {
  start: string;
  end: string;
  /** A point in time ("right now", "end of 2024") rather than a period. */
  point: boolean;
  phrase: string;
}

const MONTHS: Record<string, number> = {
  january: 1, jan: 1, february: 2, feb: 2, march: 3, mar: 3, april: 4, apr: 4, may: 5, june: 6, jun: 6,
  july: 7, jul: 7, august: 8, aug: 8, september: 9, sep: 9, sept: 9, october: 10, oct: 10,
  november: 11, nov: 11, december: 12, dec: 12,
};
const ORDINAL_QUARTER: Record<string, number> = { first: 1, "1st": 1, second: 2, "2nd": 2, third: 3, "3rd": 3, fourth: 4, "4th": 4 };
const NUMBER_WORDS: Record<string, number> = { one: 1, two: 2, three: 3, "1": 1, "2": 2, "3": 3 };

function pad(n: number): string {
  return String(n).padStart(2, "0");
}
function monthEnd(y: number, m: number): string {
  const d = new Date(Date.UTC(y, m, 0));
  return `${y}-${pad(m)}-${pad(d.getUTCDate())}`;
}
function monthRange(y: number, m: number): [string, string] {
  return [`${y}-${pad(m)}-01`, monthEnd(y, m)];
}
function quarterRange(y: number, q: number): [string, string] {
  const sm = (q - 1) * 3 + 1;
  return [`${y}-${pad(sm)}-01`, monthEnd(y, sm + 2)];
}
function yearRange(y: number): [string, string] {
  return [`${y}-01-01`, `${y}-12-31`];
}
function hint(range: [string, string], phrase: string, point = false): TimeHint {
  return { start: range[0], end: range[1], point, phrase };
}

const YEAR = "(20[2-3][0-9])";

/** Resolve the first recognizable time phrase in a question, or null. */
export function resolveTimePhrase(text: string): TimeHint | null {
  const t = ` ${text.toLowerCase().replace(/[’']/g, "'").replace(/\s+/g, " ")} `;
  const asOfYear = Number(AS_OF.slice(0, 4));
  let m: RegExpMatchArray | null;

  // explicit ranges between two years / quarters
  if ((m = t.match(new RegExp(`\\bq([1-4])\\s*(?:of\\s*)?${YEAR}\\s*(?:to|through|thru|until|-|and|vs\\.?|versus)\\s*q([1-4])\\s*(?:of\\s*)?${YEAR}`)))) {
    return hint([quarterRange(Number(m[2]), Number(m[1]))[0], quarterRange(Number(m[4]), Number(m[3]))[1]], m[0].trim());
  }
  if ((m = t.match(new RegExp(`\\b(?:between|from)?\\s*${YEAR}\\s*(?:to|through|thru|until|-|–|and|vs\\.?|versus|compared (?:to|with))\\s*${YEAR}\\b`)))) {
    const a = Number(m[1]);
    const b = Number(m[2]);
    return hint([yearRange(Math.min(a, b))[0], yearRange(Math.max(a, b))[1]], m[0].trim());
  }
  if ((m = t.match(new RegExp(`\\bsince\\s+q([1-4])\\s*(?:of\\s*)?${YEAR}`)))) {
    return hint([quarterRange(Number(m[2]), Number(m[1]))[0], AS_OF], m[0].trim());
  }
  if ((m = t.match(new RegExp(`\\bsince\\s+(?:the\\s+start\\s+of\\s+|the\\s+beginning\\s+of\\s+)?${YEAR}`)))) {
    return hint([yearRange(Number(m[1]))[0], AS_OF], m[0].trim());
  }

  // quarters
  if ((m = t.match(new RegExp(`\\bq([1-4])\\s*(?:of\\s*|'|fy)?\\s*${YEAR}\\b`)))) {
    return hint(quarterRange(Number(m[2]), Number(m[1])), m[0].trim());
  }
  if ((m = t.match(new RegExp(`\\b${YEAR}\\s*q([1-4])\\b`)))) {
    return hint(quarterRange(Number(m[1]), Number(m[2])), m[0].trim());
  }
  if ((m = t.match(new RegExp(`\\b(first|1st|second|2nd|third|3rd|fourth|4th)\\s+quarter\\s+(?:of\\s+)?${YEAR}`)))) {
    return hint(quarterRange(Number(m[2]), ORDINAL_QUARTER[m[1] ?? "first"] ?? 1), m[0].trim());
  }
  // halves
  if ((m = t.match(new RegExp(`\\bh([12])\\s*(?:of\\s*)?${YEAR}\\b`))) || (m = t.match(new RegExp(`\\b(first|second)\\s+half\\s+(?:of\\s+)?${YEAR}`)))) {
    const half = m[1] === "1" || m[1] === "first" ? 1 : 2;
    const y = Number(m[2]);
    return hint(half === 1 ? [`${y}-01-01`, `${y}-06-30`] : [`${y}-07-01`, `${y}-12-31`], m[0].trim());
  }
  // end of a month or year (a point in time)
  if ((m = t.match(new RegExp(`\\b(?:end of|as of(?: the end of)?|at the end of|close of)\\s+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\\s+${YEAR}`)))) {
    return hint(monthRange(Number(m[2]), MONTHS[m[1] ?? "jan"] ?? 1), m[0].trim(), true);
  }
  if ((m = t.match(new RegExp(`\\b(?:end of|as of(?: the end of)?|at the end of|close of|year-end|year end)\\s+${YEAR}`)))) {
    return hint(monthRange(Number(m[1]), 12), m[0].trim(), true);
  }
  // month and year
  if ((m = t.match(new RegExp(`\\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\\.?\\s+(?:of\\s+)?${YEAR}`)))) {
    return hint(monthRange(Number(m[2]), MONTHS[m[1] ?? "jan"] ?? 1), m[0].trim());
  }

  // relative phrases (as of 2025-12-31)
  if (/\b(last|past|previous|trailing)\s+(three|3)\s+years\b|\ball time\b|\bentire (period|history)\b|\bfull (period|history)\b|\bsince (the start|the beginning)\b/.test(t)) {
    return hint([DATA_START, AS_OF], "last three years");
  }
  if ((m = t.match(/\b(last|past|previous|trailing)\s+(two|2)\s+years\b/))) {
    return hint([yearRange(asOfYear - 1)[0], AS_OF], m[0].trim());
  }
  if ((m = t.match(/\b(last|past|previous|trailing)\s+(12|twelve)\s+months\b|\b(past|trailing) year\b|\bttm\b/))) {
    return hint(yearRange(asOfYear), m[0].trim());
  }
  if ((m = t.match(/\b(last|past|previous|trailing)\s+(six|6)\s+months\b/))) {
    return hint([`${asOfYear}-07-01`, AS_OF], m[0].trim());
  }
  if ((m = t.match(/\b(last|past|previous|trailing)\s+(three|3)\s+months\b/))) {
    return hint(quarterRange(asOfYear, 4), m[0].trim());
  }
  if ((m = t.match(/\blast year\b|\bprevious year\b|\bprior year\b/))) return hint(yearRange(asOfYear - 1), m[0].trim());
  if ((m = t.match(/\b(this year|year to date|year-to-date|ytd|so far this year|current year)\b/))) return hint(yearRange(asOfYear), m[0].trim());
  if ((m = t.match(/\b(last|previous|prior) quarter\b/))) return hint(quarterRange(asOfYear, 3), m[0].trim());
  if ((m = t.match(/\b(this|current) quarter\b/))) return hint(quarterRange(asOfYear, 4), m[0].trim());
  if ((m = t.match(/\b(last|previous|prior) month\b/))) return hint(monthRange(asOfYear, 11), m[0].trim());
  if ((m = t.match(/\b(this|current) month\b/))) return hint(monthRange(asOfYear, 12), m[0].trim());
  if ((m = t.match(new RegExp(`\\b(?:in|during|for|of|over|across|throughout|fy)\\s*${YEAR}\\b`))) || (m = t.match(new RegExp(`\\b${YEAR}\\b`)))) {
    return hint(yearRange(Number(m[1])), m[0].trim());
  }
  if ((m = t.match(/\b(right now|currently|today|at the moment|as of now|as of today|current|latest|at present|presently|these days)\b/))) {
    return hint(monthRange(asOfYear, 12), m[0].trim(), true);
  }
  const words = Object.keys(NUMBER_WORDS).join("|");
  if ((m = t.match(new RegExp(`\\b(last|past)\\s+(${words})\\s+quarters?\\b`)))) {
    const n = NUMBER_WORDS[m[2] ?? "1"] ?? 1;
    return hint([quarterRange(asOfYear, 5 - n)[0], AS_OF], m[0].trim());
  }
  return null;
}

/** Explicit grain words in the question ("monthly", "by quarter", "year over year"). */
export function grainHint(text: string): Grain | null {
  const t = text.toLowerCase();
  if (/\b(monthly|month by month|month over month|month-over-month|by month|per month|each month|every month|mom)\b/.test(t)) return "month";
  if (/\b(quarterly|quarter by quarter|quarter over quarter|quarter-over-quarter|by quarter|per quarter|each quarter|every quarter|qoq)\b/.test(t)) return "quarter";
  if (/\b(yearly|annually|annual|year by year|year over year|year-over-year|by year|per year|each year|every year|yoy)\b/.test(t)) return "year";
  return null;
}

/** Default period when the question names none (SPEC.md section 6). */
export function defaultRange(kind: "stock" | "flow"): { start: string; end: string } {
  return kind === "stock" ? { start: "2025-12-01", end: AS_OF } : { start: "2025-01-01", end: AS_OF };
}

/** The range to use for a metric kind given a hint: a point hint on a flow
 * metric widens to the calendar year that contains it. */
export function rangeFor(kind: "stock" | "flow", h: TimeHint | null): { start: string; end: string } {
  if (!h) return defaultRange(kind);
  if (h.point && kind === "flow") {
    const y = Number(h.end.slice(0, 4));
    return { start: `${y}-01-01`, end: `${y}-12-31` };
  }
  return { start: h.start, end: h.end };
}
