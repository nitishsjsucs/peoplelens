"""Independent reference implementation of the six governed metrics.

Plain Python over the seed CSVs: no SQL, no dbt, no MetricFlow. It mirrors the
measure definitions in SPEC.md section 6 exactly, including the non-additive
"last month of the bucket" rule, so it can serve as eval gold and as the
oracle for the 312-entry semantic parity sweep.

Conventions (identical to the dbt models):
- Active on day d: hire_date <= d and (termination_date is null or termination_date > d).
- A monthly row exists for every month in which the employee was employed at any
  point, including the month of termination (termination_date >= month_start).
- Attributes are taken as of the reference date: the month end, or the
  termination date if earlier.
- Tenure band uses whole days from hire date to month end: < 365, < 730, < 1826, else 5y+.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from peoplelens.paths import SEEDS_DIR
from peoplelens.synth import spec

GRAINS = ("none", "month", "quarter", "year")
WORKFORCE_DIMS = ("department", "location", "job_level", "employment_type", "tenure_band")
RECRUITING_DIMS = ("department", "location", "job_level")

# Governed metric id -> (MetricFlow metrics returned, cohort metric, family)
GOVERNED: dict[str, tuple[tuple[str, ...], str, str]] = {
    "headcount": (("headcount",), "headcount", "stock"),
    "fte": (("fte",), "cohort_employees", "stock"),
    "turnover_rate": (("turnover_rate",), "avg_headcount", "flow"),
    "offer_acceptance_rate": (("offer_acceptance_rate",), "offers_extended", "offers"),
    "time_to_fill_days": (("time_to_fill_days",), "filled_requisitions", "requisitions"),
    "compensation_summary": (("avg_base_salary", "median_base_salary"), "cohort_employees", "stock"),
}


@dataclass(frozen=True)
class WorkforceRow:
    month_start: date
    department: str
    location: str
    job_level: str
    employment_type: str
    tenure_band: str
    is_active_eom: int
    fte_eom: float
    is_termination: int
    base_salary_eom: int


@dataclass(frozen=True)
class RecruitingRow:
    when: date
    department: str
    location: str
    job_level: str
    value: int  # days_to_fill for requisitions, is_accepted for offers


@dataclass(frozen=True)
class Facts:
    workforce: tuple[WorkforceRow, ...]
    requisitions: tuple[RecruitingRow, ...]
    offers: tuple[RecruitingRow, ...]


@dataclass(frozen=True)
class Query:
    metric: str
    group_by: tuple[str, ...] = ()
    grain: str = "none"
    start: date = date(2025, 1, 1)
    end: date = date(2025, 12, 31)
    filters: tuple[tuple[str, tuple[str, ...]], ...] = ()
    scope_departments: tuple[str, ...] | None = None

    def filter_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.filters)


@dataclass
class Cell:
    dims: dict[str, str]
    period: str | None
    values: dict[str, float | None]
    cohort: float
    median_lower: int | None = None
    median_upper: int | None = None
    extra: dict[str, float] = field(default_factory=dict)

    def key(self) -> tuple:
        return (tuple(sorted(self.dims.items())), self.period)

    def to_json(self) -> dict:
        out = {"dims": self.dims, "period": self.period, "values": self.values, "cohort": self.cohort}
        if self.median_lower is not None:
            out["median_lower"] = self.median_lower
            out["median_upper"] = self.median_upper
        return out


# ---------------------------------------------------------------- facts


def _d(s: str) -> date | None:
    return date.fromisoformat(s) if s else None


def _read(seeds_dir: Path, name: str) -> list[dict[str, str]]:
    with open(seeds_dir / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _month_end(m: date) -> date:
    nxt = date(m.year + (m.month // 12), m.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def tenure_band(days: int) -> str:
    for bound, label in spec.TENURE_BAND_DAYS:
        if days < bound:
            return label
    return spec.TENURE_BAND_OPEN


def build_facts(seeds_dir: Path, window_start: date = spec.WINDOW_START, window_end: date = spec.WINDOW_END) -> Facts:
    emps = _read(seeds_dir, "raw_employees.csv")
    jobs: dict[str, list[dict[str, str]]] = defaultdict(list)
    for j in _read(seeds_dir, "raw_job_history.csv"):
        jobs[j["employee_id"]].append(j)
    comp: dict[str, list[tuple[date, int]]] = defaultdict(list)
    for c in _read(seeds_dir, "raw_compensation_history.csv"):
        eff = _d(c["effective_date"])
        assert eff is not None
        comp[c["employee_id"]].append((eff, int(c["base_salary_usd"])))
    for rows in comp.values():
        rows.sort()

    months = []
    m = date(window_start.year, window_start.month, 1)
    while m <= window_end:
        months.append(m)
        m = date(m.year + (m.month // 12), m.month % 12 + 1, 1)

    workforce: list[WorkforceRow] = []
    for e in emps:
        eid = e["employee_id"]
        hire = _d(e["hire_date"])
        term = _d(e["termination_date"])
        assert hire is not None
        fte = float(e["fte"])
        for ms in months:
            me = _month_end(ms)
            if hire > me or (term is not None and term < ms):
                continue
            ref = term if (term is not None and term < me) else me
            job = None
            for j in jobs[eid]:
                eff = _d(j["effective_date"])
                end = _d(j["end_date"])
                assert eff is not None
                if eff <= ref and (end is None or end >= ref):
                    job = j
            assert job is not None, (eid, ms)
            salary = None
            for eff, amount in comp[eid]:
                if eff <= ref:
                    salary = amount
            assert salary is not None, (eid, ms)
            active = 1 if (term is None or term > me) else 0
            is_term = 1 if (term is not None and ms <= term <= me) else 0
            workforce.append(
                WorkforceRow(
                    month_start=ms,
                    department=job["department"],
                    location=job["location"],
                    job_level=job["job_level"],
                    employment_type=e["employment_type"],
                    tenure_band=tenure_band((me - hire).days),
                    is_active_eom=active,
                    fte_eom=fte,
                    is_termination=is_term,
                    base_salary_eom=salary,
                )
            )

    reqs_raw = _read(seeds_dir, "raw_requisitions.csv")
    req_by_id = {r["requisition_id"]: r for r in reqs_raw}
    requisitions = []
    for r in reqs_raw:
        if r["status"] != "filled":
            continue
        filled = _d(r["filled_date"])
        opened = _d(r["opened_date"])
        assert filled is not None and opened is not None
        requisitions.append(
            RecruitingRow(filled, r["department"], r["location"], r["job_level"], (filled - opened).days)
        )
    offers = []
    for o in _read(seeds_dir, "raw_offers.csv"):
        r = req_by_id[o["requisition_id"]]
        od = _d(o["offer_date"])
        assert od is not None
        offers.append(
            RecruitingRow(od, r["department"], r["location"], r["job_level"], 1 if o["decision"] == "accepted" else 0)
        )
    return Facts(tuple(workforce), tuple(requisitions), tuple(offers))


@lru_cache(maxsize=4)
def facts_for(seeds_dir: str = str(SEEDS_DIR)) -> Facts:
    return build_facts(Path(seeds_dir))


# ---------------------------------------------------------------- evaluation


def bucket_start(d: date, grain: str) -> date | None:
    if grain == "none":
        return None
    if grain == "month":
        return date(d.year, d.month, 1)
    if grain == "quarter":
        return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    if grain == "year":
        return date(d.year, 1, 1)
    raise ValueError(f"unknown grain {grain!r}")


def _keep(row, q: Query, when: date) -> bool:
    if not (q.start <= when <= q.end):
        return False
    for dim, values in q.filters:
        if getattr(row, dim) not in values:
            return False
    if q.scope_departments is not None and row.department not in q.scope_departments:
        return False
    return True


def _group_key(row, q: Query, when: date) -> tuple[tuple[str, ...], date | None]:
    return tuple(getattr(row, d) for d in q.group_by), bucket_start(when, q.grain)


def _cell(q: Query, dims: tuple[str, ...], bucket: date | None, values: dict, cohort: float, **kw) -> Cell:
    return Cell(
        dims=dict(zip(q.group_by, dims)),
        period=bucket.isoformat() if bucket is not None else None,
        values=values,
        cohort=cohort,
        **kw,
    )


def _evaluate_stock(q: Query, facts: Facts) -> list[Cell]:
    rows = [r for r in facts.workforce if _keep(r, q, r.month_start)]
    last: dict[date | None, date] = {}
    for r in rows:
        b = bucket_start(r.month_start, q.grain)
        if b not in last or r.month_start > last[b]:
            last[b] = r.month_start
    groups: dict[tuple, list[WorkforceRow]] = defaultdict(list)
    for r in rows:
        b = bucket_start(r.month_start, q.grain)
        if r.month_start == last[b]:
            groups[_group_key(r, q, r.month_start)].append(r)
    out = []
    for (dims, bucket), grp in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or date.min)):
        actives = [r for r in grp if r.is_active_eom == 1]
        headcount = len(actives)
        if q.metric == "headcount":
            out.append(_cell(q, dims, bucket, {"headcount": float(headcount)}, float(headcount)))
        elif q.metric == "fte":
            fte = sum(r.fte_eom for r in actives)
            out.append(_cell(q, dims, bucket, {"fte": fte}, float(headcount)))
        elif q.metric == "compensation_summary":
            salaries = sorted(r.base_salary_eom for r in actives)
            if salaries:
                n = len(salaries)
                lower = salaries[(n - 1) // 2]
                upper = salaries[n // 2]
                avg = sum(salaries) / n
                med = (lower + upper) / 2
            else:
                lower = upper = None
                avg = med = None
            out.append(
                _cell(
                    q,
                    dims,
                    bucket,
                    {"avg_base_salary": avg, "median_base_salary": med},
                    float(headcount),
                    median_lower=lower,
                    median_upper=upper,
                )
            )
        else:
            raise ValueError(q.metric)
    return out


def _evaluate_turnover(q: Query, facts: Facts) -> list[Cell]:
    groups: dict[tuple, list[WorkforceRow]] = defaultdict(list)
    for r in facts.workforce:
        if _keep(r, q, r.month_start):
            groups[_group_key(r, q, r.month_start)].append(r)
    out = []
    for (dims, bucket), grp in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or date.min)):
        terms = sum(r.is_termination for r in grp)
        employee_months = sum(r.is_active_eom for r in grp)
        snapshot_months = len({r.month_start for r in grp})
        avg_headcount = employee_months / snapshot_months
        rate = terms / avg_headcount if avg_headcount else None
        out.append(
            _cell(
                q,
                dims,
                bucket,
                {"turnover_rate": rate},
                avg_headcount,
                extra={"terminations": float(terms), "employee_months": float(employee_months),
                       "snapshot_months": float(snapshot_months)},
            )
        )
    return out


def _evaluate_recruiting(q: Query, facts: Facts) -> list[Cell]:
    source = facts.offers if q.metric == "offer_acceptance_rate" else facts.requisitions
    groups: dict[tuple, list[RecruitingRow]] = defaultdict(list)
    for r in source:
        if _keep(r, q, r.when):
            groups[_group_key(r, q, r.when)].append(r)
    out = []
    for (dims, bucket), grp in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or date.min)):
        n = len(grp)
        total = sum(r.value for r in grp)
        if q.metric == "offer_acceptance_rate":
            out.append(_cell(q, dims, bucket, {"offer_acceptance_rate": total / n}, float(n)))
        else:
            out.append(_cell(q, dims, bucket, {"time_to_fill_days": total / n}, float(n)))
    return out


def evaluate(q: Query, facts: Facts | None = None) -> list[Cell]:
    """Evaluate one governed query. Groups with no rows are absent, as in SQL."""
    if q.metric not in GOVERNED:
        raise ValueError(f"unknown governed metric {q.metric!r}")
    if q.grain not in GRAINS:
        raise ValueError(f"unknown grain {q.grain!r}")
    allowed = WORKFORCE_DIMS if GOVERNED[q.metric][2] in ("stock", "flow") else RECRUITING_DIMS
    for d in q.group_by + tuple(f for f, _ in q.filters):
        if d not in allowed:
            raise ValueError(f"dimension {d!r} is not available for {q.metric}")
    facts = facts if facts is not None else facts_for()
    family = GOVERNED[q.metric][2]
    if family == "stock":
        return _evaluate_stock(q, facts)
    if family == "flow":
        return _evaluate_turnover(q, facts)
    return _evaluate_recruiting(q, facts)
