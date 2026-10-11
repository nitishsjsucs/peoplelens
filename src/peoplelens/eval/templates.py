"""Question templates: rendering text and the matching gold spec.

Time phrases are rendered here and resolved to ranges with the same rules as
the Worker's deterministic resolver (worker/src/planner/timephrases.ts); a
vitest test checks the two agree on every generated item.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from peoplelens.paths import EVAL_DIR

TEMPLATE_DIR = EVAL_DIR / "templates"
HANDWRITTEN_DIR = EVAL_DIR / "handwritten"

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

GB_PHRASES = {
    "department": ["department", "team"],
    "location": ["location", "office"],
    "job_level": ["job level", "level"],
    "employment_type": ["employment type"],
    "tenure_band": ["tenure band", "tenure"],
}
VALUE_PHRASES = {
    "department": {"Engineering": ["Engineering"], "Sales": ["Sales"], "Customer Success": ["Customer Success"],
                   "Marketing": ["Marketing"], "Finance": ["Finance"], "People": ["the People team"],
                   "Operations": ["Operations"], "Legal": ["Legal"]},
    "location": {"San Jose": ["San Jose"], "New York": ["New York", "NYC"], "Austin": ["Austin"], "London": ["London"],
                 "Bengaluru": ["Bengaluru", "Bangalore"]},
    "job_level": {lvl: [lvl] for lvl in ("L1", "L2", "L3", "L4", "L5", "L6")},
    "employment_type": {"full_time": ["full-time"], "part_time": ["part-time"]},
    "tenure_band": {"<1y": ["less than a year"], "1-2y": ["one to two years"], "2-5y": ["two to five years"],
                    "5y+": ["more than five years"]},
}
PLACEHOLDER = {"department": "dept", "location": "loc", "job_level": "level", "employment_type": "etype", "tenure_band": "tenure"}
STOCK = {"headcount", "fte", "compensation_summary"}


@dataclass
class Template:
    id: str
    metric: str
    text: str
    group_by: list[list[str]] = field(default_factory=lambda: [[]])
    filters: list[str] = field(default_factory=list)
    fixed_filters: dict[str, list[str]] = field(default_factory=dict)
    grain: str = "none"
    time: list[str] = field(default_factory=lambda: ["default"])
    fewshot: bool = False


def load_templates(name: str) -> tuple[list[Template], dict]:
    raw = yaml.safe_load((TEMPLATE_DIR / name).read_text(encoding="utf-8"))
    out = []
    for t in raw["templates"]:
        out.append(
            Template(
                id=t["id"],
                metric=t["metric"],
                text=t["text"],
                group_by=t.get("group_by") or [[]],
                filters=t.get("filters") or [],
                fixed_filters=t.get("fixed_filters") or {},
                grain=t.get("grain", "none"),
                time=t.get("time") or ["default"],
                fewshot=bool(t.get("fewshot")),
            )
        )
    return out, raw


def load_handwritten(name: str) -> list[dict]:
    return yaml.safe_load((HANDWRITTEN_DIR / name).read_text(encoding="utf-8"))["items"]


def _year_range(y: int) -> tuple[str, str]:
    return f"{y}-01-01", f"{y}-12-31"


def render_time(kind: str, metric: str, rng: random.Random) -> tuple[str, tuple[str, str]]:
    """(phrase with leading space or empty, (start, end)) as the Worker resolves it."""
    stock = metric in STOCK
    if kind == "default":
        return "", ("2025-12-01", "2025-12-31") if stock else ("2025-01-01", "2025-12-31")
    if kind == "now":
        phrase = rng.choice([" right now", " currently", " today", " at the moment"])
        return phrase, ("2025-12-01", "2025-12-31") if stock else ("2025-01-01", "2025-12-31")
    if kind == "end_of_year":
        y = rng.choice([2023, 2024, 2025])
        return f" at the end of {y}", (f"{y}-12-01", f"{y}-12-31") if stock else _year_range(y)
    if kind == "year":
        y = rng.choice([2023, 2024, 2025])
        return rng.choice([f" in {y}", f" for {y}", f" during {y}"]), _year_range(y)
    if kind == "quarter":
        y = rng.choice([2023, 2024, 2025])
        q = rng.randint(1, 4)
        sm = (q - 1) * 3 + 1
        em = sm + 2
        return f" in Q{q} {y}", (f"{y}-{sm:02d}-01", f"{y}-{em:02d}-{MONTH_DAYS[em - 1]}")
    if kind == "last_year":
        return " last year", _year_range(2024)
    if kind == "this_year":
        return rng.choice([" this year", " so far this year"]), _year_range(2025)
    if kind == "range":
        y1, y2 = rng.choice([(2023, 2024), (2024, 2025), (2023, 2025)])
        return f" from {y1} to {y2}", (f"{y1}-01-01", f"{y2}-12-31")
    if kind == "full":
        return rng.choice([" over the last three years", " since 2023"]), ("2023-01-01", "2025-12-31")
    if kind == "half":
        y = rng.choice([2023, 2024, 2025])
        h = rng.randint(1, 2)
        return f" in H{h} {y}", (f"{y}-01-01", f"{y}-06-30") if h == 1 else (f"{y}-07-01", f"{y}-12-31")
    if kind == "month":
        y = rng.choice([2023, 2024, 2025])
        m = rng.randint(1, 12)
        days = 29 if (m == 2 and y % 4 == 0) else MONTH_DAYS[m - 1]
        return f" in {MONTHS[m - 1]} {y}", (f"{y}-{m:02d}-01", f"{y}-{m:02d}-{days:02d}")
    raise ValueError(f"unknown time kind {kind!r}")


def render(t: Template, rng: random.Random, dept_choices: list[str] | None = None) -> tuple[str, dict]:
    """Render one question and its gold spec (without role or scope)."""
    group_by = list(rng.choice(t.group_by))
    values: dict[str, str] = {}
    filters: dict[str, list[str]] = {k: list(v) for k, v in t.fixed_filters.items()}
    for dim in t.filters:
        choices = sorted(VALUE_PHRASES[dim])
        if dim == "department" and dept_choices is not None:
            choices = dept_choices
        value = rng.choice(choices)
        filters[dim] = [value]
        values[PLACEHOLDER[dim]] = rng.choice(VALUE_PHRASES[dim][value])
    gb_phrases = [rng.choice(GB_PHRASES[d]) for d in group_by]
    time_kind = rng.choice(t.time)
    time_phrase, (start, end) = render_time(time_kind, t.metric, rng)
    text = t.text.format(
        time=time_phrase,
        gb=gb_phrases[0] if gb_phrases else "",
        gb2=gb_phrases[1] if len(gb_phrases) > 1 else "",
        **values,
    )
    text = text[0].upper() + text[1:]
    spec = {
        "metric": t.metric,
        "group_by": sorted(group_by),
        "grain": t.grain,
        "start": start,
        "end": end,
        "filters": {d: sorted(v) for d, v in sorted(filters.items())},
        "time_kind": time_kind,
    }
    return text, spec


def normalize_text(text: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())


def template_paths() -> list[Path]:
    return [TEMPLATE_DIR / "answerable.yaml", TEMPLATE_DIR / "dev.yaml"]
