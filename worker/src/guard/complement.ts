// Complement rule (pure planning plus a pure comparison). SPEC.md section 9.5 rule 4.
// When a question restricts a dimension to a proper subset of its values (a
// filter, or the HRBP scope), the Worker also runs the same catalog entry with
// each non-empty combination of restricted dimensions widened to every value.
// A cell whose widened counterpart differs in cohort by 1 to k-1 would let the
// asker recover a small group by subtraction (1,780 total vs 1,772 for "all but
// Legal" reveals Legal = 8), so it is suppressed. Probe results never leave the Worker.
import { widen } from "../catalog/bind";
import type { DimensionId } from "../policy/types";
import type { Validated } from "../policy/validate";
import type { Cell } from "./suppression";

export interface ProbeSpec {
  widened: DimensionId[];
  validated: Validated;
}

export function complementProbes(v: Validated): ProbeSpec[] {
  const r = [...v.restricted].sort();
  const subsets: DimensionId[][] = [];
  for (let mask = 1; mask < 1 << r.length; mask++) {
    subsets.push(r.filter((_, i) => (mask & (1 << i)) !== 0));
  }
  return subsets.map((widened) => ({ widened, validated: widen(v, widened) }));
}

const EPS = 1e-9;

export function applyComplementRule(
  main: readonly Cell[],
  probes: readonly (readonly Cell[])[],
  spec: { k: number },
): { suppressKeys: Set<string> } {
  const suppressKeys = new Set<string>();
  const probeMaps = probes.map((cells) => new Map(cells.map((c) => [c.key, c.cohort] as const)));
  for (const cell of main) {
    for (const map of probeMaps) {
      const wide = map.get(cell.key);
      if (wide === undefined) continue;
      const diff = wide - cell.cohort;
      if (diff > EPS && diff < spec.k - EPS) {
        suppressKeys.add(cell.key);
        break;
      }
    }
  }
  return { suppressKeys };
}
