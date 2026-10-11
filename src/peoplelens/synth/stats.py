"""Counts computed from the seed CSVs on disk (independent of the generator)."""

from __future__ import annotations

import csv
from collections import Counter
from datetime import date
from pathlib import Path

from peoplelens.paths import SEEDS_DIR
from peoplelens.synth import spec


def read_csv(name: str, seeds_dir: Path = SEEDS_DIR) -> list[dict[str, str]]:
    with open(seeds_dir / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _d(s: str) -> date | None:
    return date.fromisoformat(s) if s else None


def _active(hire: date, term: date | None, d: date) -> bool:
    return hire <= d and (term is None or term > d)


def compute(seeds_dir: Path = SEEDS_DIR) -> dict[str, object]:
    emps = read_csv("raw_employees.csv", seeds_dir)
    jobs = read_csv("raw_job_history.csv", seeds_dir)
    comp = read_csv("raw_compensation_history.csv", seeds_dir)
    reqs = read_csv("raw_requisitions.csv", seeds_dir)
    offers = read_csv("raw_offers.csv", seeds_dir)
    cands = read_csv("raw_candidates.csv", seeds_dir)

    closing_ids = set()
    opening = 0
    hires_by_year: Counter[int] = Counter()
    terms_by_year: Counter[int] = Counter()
    term_types: Counter[str] = Counter()
    closing_fte_tenths = 0
    closing_part_time: Counter[str] = Counter()
    for e in emps:
        hire = _d(e["hire_date"])
        term = _d(e["termination_date"])
        assert hire is not None
        if hire < spec.WINDOW_START and (term is None or term >= spec.WINDOW_START):
            opening += 1
        if spec.WINDOW_START <= hire <= spec.WINDOW_END:
            hires_by_year[hire.year] += 1
        if term is not None and spec.WINDOW_START <= term <= spec.WINDOW_END:
            terms_by_year[term.year] += 1
            term_types[e["termination_type"]] += 1
        if _active(hire, term, spec.AS_OF):
            closing_ids.add(e["employee_id"])
            closing_fte_tenths += int(e["fte"].replace(".", ""))
            if e["employment_type"] == "part_time":
                closing_part_time[e["fte"]] += 1

    current: dict[str, dict[str, str]] = {}
    for j in jobs:
        eff = _d(j["effective_date"])
        assert eff is not None
        if eff <= spec.AS_OF:
            prev = current.get(j["employee_id"])
            if prev is None or prev["effective_date"] < j["effective_date"]:
                current[j["employee_id"]] = j
    by_dept: Counter[str] = Counter()
    by_loc: Counter[str] = Counter()
    by_level: Counter[str] = Counter()
    for eid in closing_ids:
        row = current[eid]
        by_dept[row["department"]] += 1
        by_loc[row["location"]] += 1
        by_level[row["job_level"]] += 1

    def _in_window(s: str) -> bool:
        d = _d(s)
        return d is not None and spec.WINDOW_START <= d <= spec.WINDOW_END

    reasons = Counter(j["change_reason"] for j in jobs if j["change_reason"] == "hire" or _in_window(j["effective_date"]))
    req_status = Counter(r["status"] for r in reqs)
    decisions = Counter(o["decision"] for o in offers)
    filled_req_ids = {r["requisition_id"] for r in reqs if r["status"] == "filled"}
    hired_ids = [o["hired_employee_id"] for o in offers if o["decision"] == "accepted"]
    window_hire_ids = {e["employee_id"] for e in emps if spec.WINDOW_START <= date.fromisoformat(e["hire_date"])}
    comp_reasons = Counter(c["change_reason"] for c in comp)

    return {
        "employees": len(emps),
        "opening_headcount": opening,
        "hires_by_year": dict(sorted(hires_by_year.items())),
        "hires": sum(hires_by_year.values()),
        "terminations_by_year": dict(sorted(terms_by_year.items())),
        "terminations": sum(terms_by_year.values()),
        "voluntary": term_types["voluntary"],
        "involuntary": term_types["involuntary"],
        "closing_headcount": len(closing_ids),
        "closing_part_time_half": closing_part_time[spec.FTE_HALF],
        "closing_part_time_four_fifths": closing_part_time[spec.FTE_FOUR_FIFTHS],
        "closing_fte": closing_fte_tenths / 10,
        "closing_by_department": {k: by_dept[k] for k in spec.DEPARTMENTS},
        "closing_by_location": {k: by_loc[k] for k in spec.LOCATIONS},
        "closing_by_level": {k: by_level[k] for k in spec.JOB_LEVELS},
        "job_history_rows": len(jobs),
        "promotions": reasons["promotion"],
        "transfers": reasons["transfer"],
        "compensation_rows": len(comp),
        "compensation_by_reason": dict(sorted(comp_reasons.items())),
        "requisitions": len(reqs),
        "requisitions_by_status": dict(sorted(req_status.items())),
        "offers": len(offers),
        "offers_accepted": decisions["accepted"],
        "offers_declined": decisions["declined"],
        "candidates": len(cands),
        "filled_requisitions_match_hires": (
            len(filled_req_ids) == len(window_hire_ids) == len(set(hired_ids)) == len(hired_ids)
            and set(hired_ids) == window_hire_ids
        ),
    }
