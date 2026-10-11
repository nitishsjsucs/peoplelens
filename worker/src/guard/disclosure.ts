// Disclosure rounding (pure). SPEC.md section 9.5 rule 7.
// Suppression keeps every shown cohort at k or more, but two allowed questions can
// still differ by one person: compensation by department and level for November and
// for December, when one person joined a cohort of 52, gives n2 x avg2 - n1 x avg1 =
// that person's exact salary. Measures listed in a metric's `disclosure_round` (both
// compensation measures, to the nearest $1,000) are therefore rounded after
// suppression and before the rows and the narrative are built. The same
// differencing then lands thousands of dollars away from the real salary
// (tests/py/test_disclosure.py measures it on the seeded data).
import type { MetricDef } from "../policy/types";
import type { OutputRow } from "./suppression";

/** Round half up to a multiple of `step` (Math.round semantics). */
export function roundTo(value: number, step: number): number {
  return Math.round(value / step) * step;
}

export function roundForDisclosure(rows: readonly OutputRow[], metric: MetricDef): OutputRow[] {
  const steps = metric.disclosure_round;
  if (!steps || Object.keys(steps).length === 0) return [...rows];
  return rows.map((r) => {
    const values: Record<string, number | null> = { ...r.values };
    for (const [m, step] of Object.entries(steps)) {
      const v = values[m];
      if (typeof v === "number" && Number.isFinite(v) && step > 0) values[m] = roundTo(v, step);
    }
    return { ...r, values };
  });
}
