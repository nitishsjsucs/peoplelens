// A fake Warehouse for API tests: evaluates catalog entries against an in-memory
// population with the same semantics as the governed metrics for headcount, and
// records every call so tests can assert what was (or was not) executed.
import type { BoundQuery } from "../../src/catalog/bind";
import type { CellValue, RunOptions, RunResult, Warehouse } from "../../src/warehouse/types";

export interface Person {
  department: string;
  location: string;
  job_level: string;
  employment_type: string;
  tenure_band: string;
}

const DEPTS: [string, number][] = [
  ["Engineering", 620], ["Sales", 380], ["Customer Success", 250], ["Marketing", 150],
  ["Finance", 120], ["People", 90], ["Operations", 162], ["Legal", 8],
];
const LOCS = ["San Jose", "New York", "Austin", "London", "Bengaluru"];
const LEVELS = ["L1", "L2", "L3", "L4", "L5", "L6"];
const TENURE = ["<1y", "1-2y", "2-5y", "5y+"];

/** 1,780 people with the closing department marginals (Legal = 8). */
export function closingPopulation(): Person[] {
  const out: Person[] = [];
  let i = 0;
  for (const [dept, n] of DEPTS) {
    for (let j = 0; j < n; j++, i++) {
      out.push({
        department: dept,
        location: LOCS[i % LOCS.length] ?? "San Jose",
        job_level: LEVELS[(i * 7) % LEVELS.length] ?? "L1",
        employment_type: i % 11 === 0 ? "part_time" : "full_time",
        tenure_band: TENURE[(i * 3) % TENURE.length] ?? "<1y",
      });
    }
  }
  return out;
}

export interface FakeCall {
  kind: "dryRun" | "run";
  q: BoundQuery;
  o?: RunOptions;
}

export class FakeWarehouse implements Warehouse {
  readonly kind = "fake" as const;
  readonly calls: FakeCall[] = [];
  constructor(
    private readonly people: Person[] = closingPopulation(),
    private readonly bytesPerQuery = 10 * 1024 * 1024,
  ) {}

  async dryRun(q: BoundQuery): Promise<{ estimatedBytes: number }> {
    this.calls.push({ kind: "dryRun", q });
    return { estimatedBytes: this.bytesPerQuery };
  }

  async run(q: BoundQuery, o: RunOptions): Promise<RunResult> {
    this.calls.push({ kind: "run", q, o });
    const entry = q.entry;
    const accepts = (p: Person): boolean => {
      for (const [name, param] of Object.entries(q.params)) {
        if (param.type !== "ARRAY<STRING>") continue;
        if (name === "scope_departments") {
          if (!param.value.includes(p.department)) return false;
          continue;
        }
        const dim = name.slice(2) as keyof Person;
        if (!param.value.includes("__all__") && !param.value.includes(p[dim])) return false;
      }
      return true;
    };
    const groups = new Map<string, { dims: string[]; n: number }>();
    for (const p of this.people.filter(accepts)) {
      const dims = entry.group_by.map((d) => p[d as keyof Person]);
      const key = JSON.stringify(dims);
      const g = groups.get(key) ?? { dims, n: 0 };
      g.n += 1;
      groups.set(key, g);
    }
    const dimCols = entry.group_by.map((d) => entry.columns.dimensions[d] ?? d);
    const timeCols = entry.columns.time ? [entry.columns.time] : [];
    const metricCols = entry.mf_metrics;
    const rows: CellValue[][] = [...groups.values()].map((g) => [
      ...g.dims,
      ...timeCols.map(() => "2025-12-01"),
      ...metricCols.map((m) => (m === entry.columns.cohort ? g.n : m === "headcount" ? g.n : g.n * 1.5)),
    ]);
    if (entry.group_by.length === 0 && rows.length === 0) rows.push([...timeCols.map(() => null), ...metricCols.map(() => null)]);
    return { columns: [...dimCols, ...timeCols, ...metricCols], rows, billedBytes: this.bytesPerQuery, elapsedMs: 1 };
  }
}
