"""Seeded synthetic HR dataset for the fictional company Lumen Works.

The generator builds the end state first (who is active on 2025-12-31, with
which attributes) so that every marginal in SPEC.md section 5.3 holds by
construction, then back-fills hire dates, terminations, promotions, transfers,
compensation and recruiting history.

Determinism rules: one ``random.Random(SEED)``, integer arithmetic only,
sorted iteration, no hash-ordered containers, fixed string formatting. The
CSV bytes are identical on macOS and Linux.
"""

from __future__ import annotations

import csv
import hashlib
import io
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from peoplelens.synth import spec
from peoplelens.synth.names import FIRST_NAMES, GENDER_WEIGHTS, GENDERS, LAST_NAMES

COLUMNS: dict[str, tuple[str, ...]] = {
    "raw_employees.csv": (
        "employee_id", "full_name", "work_email", "date_of_birth", "gender", "hire_date",
        "termination_date", "termination_type", "employment_type", "fte",
    ),
    "raw_job_history.csv": (
        "employee_id", "effective_date", "end_date", "department", "location", "job_level",
        "job_family", "change_reason",
    ),
    "raw_compensation_history.csv": ("employee_id", "effective_date", "base_salary_usd", "change_reason"),
    "raw_requisitions.csv": (
        "requisition_id", "department", "location", "job_level", "opened_date", "status",
        "filled_date", "closed_date",
    ),
    "raw_offers.csv": (
        "offer_id", "requisition_id", "candidate_id", "offer_date", "decision", "decision_date",
        "hired_employee_id",
    ),
    "raw_candidates.csv": ("candidate_id", "full_name", "email"),
}


@dataclass
class _State:
    department: str
    location: str
    job_level: str
    job_family: str


@dataclass
class _Person:
    seq: int
    final: _State
    fte: str
    hire_date: date = date.min
    termination_date: date | None = None
    termination_type: str = ""
    promotion_date: date | None = None
    transfer_date: date | None = None
    initial: _State | None = None
    rows: list[tuple[date, _State, str]] = field(default_factory=list)
    employee_id: str = ""
    full_name: str = ""
    first: str = ""
    last: str = ""


# ---------------------------------------------------------------- helpers


def _expand(counts: dict[str, int]) -> list[str]:
    out: list[str] = []
    for key, n in counts.items():
        out.extend([key] * n)
    return out


def _apportion(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Largest-remainder apportionment with integer arithmetic; ties by key order."""
    wsum = sum(weights.values())
    base = {k: (total * w) // wsum for k, w in weights.items()}
    rem = total - sum(base.values())
    order = sorted(weights, key=lambda k: (-((total * weights[k]) % wsum), list(weights).index(k)))
    for k in order[:rem]:
        base[k] += 1
    return base


def _rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def _weighted(rng: random.Random, items: tuple[str, ...], weights: tuple[int, ...]) -> str:
    r = rng.randrange(sum(weights))
    acc = 0
    for item, w in zip(items, weights):
        acc += w
        if r < acc:
            return item
    raise AssertionError("unreachable")


def _month_starts(start: date, end: date) -> list[date]:
    out = []
    d = date(start.year, start.month, 1)
    while d <= end:
        out.append(d)
        d = date(d.year + (d.month // 12), d.month % 12 + 1, 1)
    return out


def _family(rng: random.Random, department: str) -> str:
    fams = spec.JOB_FAMILIES[department]
    return fams[rng.randrange(len(fams))]


def _round_salary(value: int) -> int:
    return (value + 50) // 100 * 100


def _apply_bp(salary: int, bp: int) -> int:
    return _round_salary(salary * (10000 + bp) // 10000)


def is_active(hire: date, term: date | None, d: date) -> bool:
    """Active on day d: hired on or before d and not yet terminated (termination day is not active)."""
    return hire <= d and (term is None or term > d)


# ---------------------------------------------------------------- generator


def generate() -> dict[str, list[dict[str, str]]]:
    rng = random.Random(spec.SEED)

    # 1. Closing actives: exact department, location, level and FTE marginals.
    depts = _expand(spec.CLOSING_BY_DEPARTMENT)
    locs = _expand(spec.CLOSING_BY_LOCATION)
    levels = _expand(spec.CLOSING_BY_LEVEL)
    ftes = (
        [spec.FTE_HALF] * spec.CLOSING_PART_TIME_HALF
        + [spec.FTE_FOUR_FIFTHS] * spec.CLOSING_PART_TIME_FOUR_FIFTHS
    )
    ftes += [spec.FTE_FULL] * (spec.CLOSING_HEADCOUNT - len(ftes))
    rng.shuffle(locs)
    rng.shuffle(levels)
    rng.shuffle(ftes)
    people: list[_Person] = []
    for i in range(spec.CLOSING_HEADCOUNT):
        st = _State(depts[i], locs[i], levels[i], _family(rng, depts[i]))
        people.append(_Person(seq=len(people), final=st, fte=ftes[i]))
    closing = list(people)

    # 2. Leavers: final attributes apportioned from the closing proportions.
    n_term = sum(spec.TERMINATIONS_BY_YEAR.values())
    t_depts = _expand(_apportion(n_term, spec.CLOSING_BY_DEPARTMENT))
    t_locs = _expand(_apportion(n_term, spec.CLOSING_BY_LOCATION))
    t_levels = _expand(_apportion(n_term, spec.CLOSING_BY_LEVEL))
    t_ftes = (
        [spec.FTE_HALF] * spec.TERMINATED_PART_TIME_HALF
        + [spec.FTE_FOUR_FIFTHS] * spec.TERMINATED_PART_TIME_FOUR_FIFTHS
    )
    t_ftes += [spec.FTE_FULL] * (n_term - len(t_ftes))
    rng.shuffle(t_locs)
    rng.shuffle(t_levels)
    rng.shuffle(t_ftes)
    leavers: list[_Person] = []
    for i in range(n_term):
        st = _State(t_depts[i], t_locs[i], t_levels[i], _family(rng, t_depts[i]))
        p = _Person(seq=len(people), final=st, fte=t_ftes[i])
        people.append(p)
        leavers.append(p)

    # 3. Hire and termination years.
    n_term_hires = sum(spec.TERMINATED_HIRES.values())
    stay_hires_by_year = {
        y: spec.HIRES_BY_YEAR[y] - sum(n for (hy, _), n in spec.TERMINATED_HIRES.items() if hy == y)
        for y in spec.WINDOW_YEARS
    }
    closing_order = list(range(len(closing)))
    rng.shuffle(closing_order)
    cursor = 0
    for y in spec.WINDOW_YEARS:
        for idx in closing_order[cursor : cursor + stay_hires_by_year[y]]:
            p = closing[idx]
            p.hire_date = _rand_date(rng, date(y, 1, 1), date(y, 12, 31))
        cursor += stay_hires_by_year[y]
    for idx in closing_order[cursor:]:
        closing[idx].hire_date = _rand_date(rng, spec.OPENER_HIRE_START, spec.OPENER_HIRE_END)

    leaver_order = list(range(len(leavers)))
    rng.shuffle(leaver_order)
    cursor = 0
    for (hy, ty), n in spec.TERMINATED_HIRES.items():
        for idx in leaver_order[cursor : cursor + n]:
            p = leavers[idx]
            if hy == ty:
                p.hire_date = _rand_date(rng, date(hy, 1, 1), date(hy, 9, 30))
                p.termination_date = _rand_date(rng, p.hire_date + timedelta(days=30), date(ty, 12, 31))
            else:
                p.hire_date = _rand_date(rng, date(hy, 1, 1), date(hy, 12, 31))
                p.termination_date = _rand_date(rng, date(ty, 1, 1), date(ty, 12, 31))
        cursor += n
    hire_term_by_year = {
        y: sum(n for (_, ty), n in spec.TERMINATED_HIRES.items() if ty == y) for y in spec.WINDOW_YEARS
    }
    for y in spec.WINDOW_YEARS:
        n = spec.TERMINATIONS_BY_YEAR[y] - hire_term_by_year[y]
        for idx in leaver_order[cursor : cursor + n]:
            p = leavers[idx]
            p.hire_date = _rand_date(rng, spec.OPENER_HIRE_START, spec.OPENER_HIRE_END)
            p.termination_date = _rand_date(rng, date(y, 1, 1), date(y, 12, 31))
        cursor += n
    assert cursor == n_term and n_term_hires <= n_term

    types = ["voluntary"] * spec.VOLUNTARY_TERMINATIONS + ["involuntary"] * spec.INVOLUNTARY_TERMINATIONS
    rng.shuffle(types)
    for p, t in zip(leavers, types):
        p.termination_type = t

    # 4. Promotions and transfers inside the window, on the first of a month.
    months = _month_starts(spec.WINDOW_START, spec.WINDOW_END)

    def _valid_dates(p: _Person, exclude_april: bool) -> list[date]:
        out = []
        for m in months:
            if exclude_april and m.month == spec.MERIT_MONTH_DAY[0]:
                continue
            if m < p.hire_date + timedelta(days=183):
                continue
            if p.termination_date is not None and m > p.termination_date - timedelta(days=30):
                continue
            out.append(m)
        return out

    promo_pool = [p for p in people if p.final.job_level != "L1" and _valid_dates(p, True)]
    for p in rng.sample(promo_pool, spec.PROMOTIONS):
        options = _valid_dates(p, True)
        p.promotion_date = options[rng.randrange(len(options))]

    transfer_pool = [p for p in people if p.final.department != "Legal" and len(_valid_dates(p, False)) >= 2]
    for p in rng.sample(transfer_pool, spec.TRANSFERS):
        options = [d for d in _valid_dates(p, False) if d != p.promotion_date]
        p.transfer_date = options[rng.randrange(len(options))]

    # 5. Job history, built backwards from the final state.
    for p in people:
        events: list[tuple[date, str]] = []
        if p.promotion_date is not None:
            events.append((p.promotion_date, "promotion"))
        if p.transfer_date is not None:
            events.append((p.transfer_date, "transfer"))
        events.sort()
        state = p.final
        rows_rev: list[tuple[date, _State, str]] = []
        for when, kind in reversed(events):
            rows_rev.append((when, state, kind))
            if kind == "promotion":
                prev_level = spec.JOB_LEVELS[spec.JOB_LEVELS.index(state.job_level) - 1]
                state = _State(state.department, state.location, prev_level, state.job_family)
            else:
                others = [d for d in spec.DEPARTMENTS if d not in (state.department, "Legal")]
                prev_dept = others[rng.randrange(len(others))]
                state = _State(prev_dept, state.location, state.job_level, _family(rng, prev_dept))
        rows_rev.append((p.hire_date, state, "hire"))
        p.rows = list(reversed(rows_rev))
        p.initial = state

    # 6. Identity: employee ids in hire order, names, emails, birth dates.
    people_sorted = sorted(people, key=lambda q: (q.hire_date, q.seq))
    used_emails: dict[str, int] = {}
    employees: list[dict[str, str]] = []
    for n, p in enumerate(people_sorted, start=1):
        p.employee_id = f"E{n:05d}"
        p.first = FIRST_NAMES[rng.randrange(len(FIRST_NAMES))]
        p.last = LAST_NAMES[rng.randrange(len(LAST_NAMES))]
        p.full_name = f"{p.first} {p.last}"
        local = f"{p.first}.{p.last}".lower()
        used_emails[local] = used_emails.get(local, 0) + 1
        if used_emails[local] > 1:
            local = f"{local}{used_emails[local]}"
        # Age at hire 22 to 58, and nobody older than 66 at the as-of date.
        max_age_days = min(58 * 365, 66 * 365 - (spec.AS_OF - p.hire_date).days)
        age_days = rng.randint(22 * 365, max(23 * 365, max_age_days))
        dob = p.hire_date - timedelta(days=age_days)
        gender = _weighted(rng, GENDERS, GENDER_WEIGHTS)
        employees.append(
            {
                "employee_id": p.employee_id,
                "full_name": p.full_name,
                "work_email": f"{local}@{spec.EMAIL_DOMAIN}",
                "date_of_birth": dob.isoformat(),
                "gender": gender,
                "hire_date": p.hire_date.isoformat(),
                "termination_date": p.termination_date.isoformat() if p.termination_date else "",
                "termination_type": p.termination_type,
                "employment_type": "full_time" if p.fte == spec.FTE_FULL else "part_time",
                "fte": p.fte,
            }
        )

    job_history: list[dict[str, str]] = []
    for p in people_sorted:
        for i, (when, st, kind) in enumerate(p.rows):
            if i + 1 < len(p.rows):
                end = (p.rows[i + 1][0] - timedelta(days=1)).isoformat()
            else:
                end = p.termination_date.isoformat() if p.termination_date else ""
            job_history.append(
                {
                    "employee_id": p.employee_id,
                    "effective_date": when.isoformat(),
                    "end_date": end,
                    "department": st.department,
                    "location": st.location,
                    "job_level": st.job_level,
                    "job_family": st.job_family,
                    "change_reason": kind,
                }
            )

    # 7. Compensation: hire row, April 1 merit rows inside the window, promotion rows.
    comp: list[dict[str, str]] = []
    for p in people_sorted:
        init = p.initial
        assert init is not None
        salary = spec.BASE_SALARY_BY_LEVEL[init.job_level]
        salary = salary * spec.DEPARTMENT_PAY_BP[init.department] // 10000
        salary = salary * spec.LOCATION_PAY_BP[init.location] // 10000
        salary = _round_salary(salary * rng.randint(*spec.SALARY_SPREAD_BP) // 10000)
        events: list[tuple[date, str]] = [(p.hire_date, "hire")]
        for y in spec.WINDOW_YEARS:
            merit_day = date(y, *spec.MERIT_MONTH_DAY)
            eligible_from = date(y - 1, merit_day.month + 12 - spec.MERIT_MIN_TENURE_MONTHS, 1)
            if p.hire_date <= eligible_from and is_active(p.hire_date, p.termination_date, merit_day):
                events.append((merit_day, "merit"))
        if p.promotion_date is not None:
            events.append((p.promotion_date, "promotion"))
        events.sort()
        for when, kind in events:
            if kind == "merit":
                salary = _apply_bp(salary, rng.randint(*spec.MERIT_BP))
            elif kind == "promotion":
                salary = _apply_bp(salary, rng.randint(*spec.PROMOTION_BP))
            comp.append(
                {
                    "employee_id": p.employee_id,
                    "effective_date": when.isoformat(),
                    "base_salary_usd": str(salary),
                    "change_reason": kind,
                }
            )

    # 8. Requisitions: one per window hire (filled on the hire date), plus open and cancelled.
    window_hires = [p for p in people_sorted if p.hire_date >= spec.WINDOW_START]
    assert len(window_hires) == spec.REQUISITIONS_FILLED
    reqs: list[dict] = []
    for p in window_hires:
        init = p.initial
        assert init is not None
        lo, hi = spec.TIME_TO_FILL_DAYS[init.job_level]
        opened = p.hire_date - timedelta(days=rng.randint(lo, hi))
        assert opened >= spec.REQ_OPEN_START
        reqs.append(
            {"seq": len(reqs), "department": init.department, "location": init.location,
             "job_level": init.job_level, "opened_date": opened, "status": "filled",
             "filled_date": p.hire_date, "closed_date": p.hire_date, "hire": p}
        )
    closing_states = [p.final for p in closing]
    for _ in range(spec.REQUISITIONS_OPEN):
        st = closing_states[rng.randrange(len(closing_states))]
        reqs.append(
            {"seq": len(reqs), "department": st.department, "location": st.location,
             "job_level": st.job_level, "opened_date": _rand_date(rng, date(2025, 6, 1), date(2025, 12, 15)),
             "status": "open", "filled_date": None, "closed_date": None, "hire": None}
        )
    for _ in range(spec.REQUISITIONS_CANCELLED):
        st = closing_states[rng.randrange(len(closing_states))]
        opened = _rand_date(rng, spec.REQ_OPEN_START, date(2025, 8, 31))
        reqs.append(
            {"seq": len(reqs), "department": st.department, "location": st.location,
             "job_level": st.job_level, "opened_date": opened, "status": "cancelled",
             "filled_date": None, "closed_date": opened + timedelta(days=rng.randint(20, 120)), "hire": None}
        )
    reqs.sort(key=lambda r: (r["opened_date"], r["seq"]))
    for n, r in enumerate(reqs, start=1):
        r["requisition_id"] = f"R{n:05d}"

    # 9. Offers and candidates.
    offers: list[dict] = []
    filled = [r for r in reqs if r["status"] == "filled"]
    for r in filled:
        p = r["hire"]
        offer_date = r["filled_date"] - timedelta(days=rng.randint(14, 28))
        offers.append(
            {"req": r, "offer_date": offer_date, "decision": "accepted",
             "decision_date": offer_date + timedelta(days=rng.randint(1, 5)), "person": p}
        )
    accepted_by_req = {o["req"]["requisition_id"]: o["offer_date"] for o in offers}
    for r in rng.sample(filled, spec.DECLINED_ON_FILLED):
        start = r["opened_date"] + timedelta(days=1)
        end = accepted_by_req[r["requisition_id"]] - timedelta(days=6)
        od = _rand_date(rng, start, end)
        offers.append({"req": r, "offer_date": od, "decision": "declined",
                       "decision_date": od + timedelta(days=rng.randint(1, 5)), "person": None})
    open_reqs = [r for r in reqs if r["status"] == "open"]
    for r in rng.sample(open_reqs, spec.DECLINED_ON_OPEN):
        start = r["opened_date"] + timedelta(days=7)
        end = min(r["opened_date"] + timedelta(days=60), date(2025, 12, 24))
        od = _rand_date(rng, start, end)
        offers.append({"req": r, "offer_date": od, "decision": "declined",
                       "decision_date": od + timedelta(days=rng.randint(1, 5)), "person": None})
    cancelled = [r for r in reqs if r["status"] == "cancelled"]
    for r in rng.sample(cancelled, spec.DECLINED_ON_CANCELLED):
        start = r["opened_date"] + timedelta(days=7)
        end = r["closed_date"] - timedelta(days=6)
        od = _rand_date(rng, start, end)
        offers.append({"req": r, "offer_date": od, "decision": "declined",
                       "decision_date": od + timedelta(days=rng.randint(1, 5)), "person": None})
    offers.sort(key=lambda o: (o["offer_date"], o["req"]["requisition_id"], o["decision"]))

    offer_rows: list[dict[str, str]] = []
    candidates: list[dict[str, str]] = []
    used_cand: dict[str, int] = {}
    for n, o in enumerate(offers, start=1):
        cid = f"C{n:05d}"
        p = o["person"]
        if p is not None:
            first, last = p.first, p.last
        else:
            first = FIRST_NAMES[rng.randrange(len(FIRST_NAMES))]
            last = LAST_NAMES[rng.randrange(len(LAST_NAMES))]
        local = f"{first}.{last}".lower()
        used_cand[local] = used_cand.get(local, 0) + 1
        if used_cand[local] > 1:
            local = f"{local}{used_cand[local]}"
        candidates.append({"candidate_id": cid, "full_name": f"{first} {last}",
                           "email": f"{local}@{spec.CANDIDATE_EMAIL_DOMAIN}"})
        offer_rows.append(
            {
                "offer_id": f"O{n:05d}",
                "requisition_id": o["req"]["requisition_id"],
                "candidate_id": cid,
                "offer_date": o["offer_date"].isoformat(),
                "decision": o["decision"],
                "decision_date": o["decision_date"].isoformat(),
                "hired_employee_id": p.employee_id if p is not None else "",
            }
        )

    req_rows = [
        {
            "requisition_id": r["requisition_id"],
            "department": r["department"],
            "location": r["location"],
            "job_level": r["job_level"],
            "opened_date": r["opened_date"].isoformat(),
            "status": r["status"],
            "filled_date": r["filled_date"].isoformat() if r["filled_date"] else "",
            "closed_date": r["closed_date"].isoformat() if r["closed_date"] else "",
        }
        for r in reqs
    ]

    return {
        "raw_employees.csv": employees,
        "raw_job_history.csv": job_history,
        "raw_compensation_history.csv": comp,
        "raw_requisitions.csv": req_rows,
        "raw_offers.csv": offer_rows,
        "raw_candidates.csv": candidates,
    }


def to_csv_bytes(name: str, rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=list(COLUMNS[name]))
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def render() -> dict[str, bytes]:
    tables = generate()
    return {name: to_csv_bytes(name, tables[name]) for name in spec.SEED_FILES}


def sha256sums(files: dict[str, bytes]) -> bytes:
    lines = [f"{hashlib.sha256(files[name]).hexdigest()}  {name}\n" for name in sorted(files)]
    return "".join(lines).encode("ascii")
