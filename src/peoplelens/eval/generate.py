"""`pl-eval generate`: build eval/questions.jsonl (exactly 200 items, 214 turns)
and eval/dev_questions.jsonl (exactly 60 items) deterministically, then freeze
them in git. Answerable items are kept only when every gold cell has a cohort
of at least 10 and nothing would be suppressed (so the expected answer is fully
visible), and only for roles allowed to ask them.
"""

from __future__ import annotations

import json
import random
from collections import Counter

from peoplelens.eval.gold import gold_for_spec
from peoplelens.eval.templates import Template, load_handwritten, load_templates, normalize_text, render
from peoplelens.paths import EVAL_DIR
from peoplelens.policy import load

SEED = 20231
PER_METRIC = 25
DEV_ITEMS = 60
DEV_TEMPLATE_ITEMS = 57
QUESTIONS_PATH = EVAL_DIR / "questions.jsonl"
DEV_PATH = EVAL_DIR / "dev_questions.jsonl"
ROLE_ORDER = ("people_analytics", "hr_business_partner", "talent_acquisition", "executive")
HRBP_SCOPE = ["Engineering", "Sales"]
ADVERSARIAL_CATEGORIES = {
    "prompt_injection": 6,
    "small_cohort": 4,
    "differencing": 4,
    "unapproved_dimension_or_pii": 5,
    "scope": 4,
    "write": 3,
    "cost": 4,
}


def _emails() -> dict[str, str]:
    from peoplelens.dev.tokens import email_for_role

    return {r: email_for_role(r) for r in ROLE_ORDER}


def permitted_roles(spec: dict) -> list[str]:
    policy = load()
    dims = set(spec["group_by"]) | set(spec["filters"])
    out = []
    for rid in ROLE_ORDER:
        role = policy.roles[rid]
        if spec["metric"] not in role["metrics"] or not dims <= set(role["dimensions"]) or spec["grain"] not in role["grains"]:
            continue
        if rid == "hr_business_partner" and not set(spec["filters"].get("department", HRBP_SCOPE)) <= set(HRBP_SCOPE):
            continue
        out.append(rid)
    return out


def _row_bound(spec: dict) -> int:
    policy = load()
    sy, sm = int(spec["start"][:4]), int(spec["start"][5:7])
    ey, em = int(spec["end"][:4]), int(spec["end"][5:7])
    buckets = {
        "none": 1,
        "month": (ey - sy) * 12 + (em - sm) + 1,
        "quarter": (ey - sy) * 4 + ((em - 1) // 3 - (sm - 1) // 3) + 1,
        "year": ey - sy + 1,
    }[spec["grain"]]
    rows = buckets
    for d in spec["group_by"]:
        vals = spec["filters"].get(d) or policy.dimensions[d]["values"]
        if d == "department" and spec.get("scope"):
            vals = [v for v in vals if v in spec["scope"]]
        rows *= len(vals)
    return rows


def acceptable(spec: dict) -> bool:
    """Every gold cell visible: cohort >= 10, no suppression of any kind, within limits."""
    limits = load().limits
    if len(spec["group_by"]) > limits["max_group_by"] or _row_bound(spec) > limits["max_rows"]:
        return False
    gold = gold_for_spec(spec)
    if gold["restricted"] and len(gold["restricted"]) > limits["max_restricted_dimensions"]:
        return False
    cells = gold["cells"]
    return bool(cells) and all(c["cohort"] >= limits["min_cohort"] and not c["expected_suppressed"] for c in cells)


def _spec_for_role(spec: dict, role: str) -> dict:
    out = {k: v for k, v in spec.items() if k != "time_kind"}
    if role == "hr_business_partner":
        out["scope"] = list(HRBP_SCOPE)
    return out


def _generate_from_templates(templates: list[Template], per_metric: dict[str, int], rng: random.Random,
                             prefix: str, seen: set[str], max_tries: int = 40) -> list[dict]:
    emails = _emails()
    items: list[dict] = []
    role_counts: Counter[str] = Counter()
    by_metric: dict[str, list[Template]] = {}
    for t in templates:
        by_metric.setdefault(t.metric, []).append(t)
    for metric, want in per_metric.items():
        pool = by_metric.get(metric, [])
        made = 0
        cursor = 0
        stalls = 0
        while made < want:
            if stalls > len(pool) * 3:
                raise RuntimeError(f"cannot generate {want} {prefix} items for {metric}; made {made}")
            t = pool[cursor % len(pool)]
            cursor += 1
            produced = False
            for _ in range(max_tries):
                dept_choices = None
                text, spec = render(t, rng, dept_choices)
                roles = permitted_roles(spec)
                if not roles:
                    continue
                # spread items across permitted roles: least used first, ties in role order
                role = sorted(roles, key=lambda r: (role_counts[r], ROLE_ORDER.index(r)))[0]
                if role == "hr_business_partner" and "department" in t.filters:
                    text, spec = render(t, rng, HRBP_SCOPE)
                    if "hr_business_partner" not in permitted_roles(spec):
                        continue
                full = _spec_for_role(spec, role)
                norm = normalize_text(text)
                if norm in seen or not acceptable(full):
                    continue
                seen.add(norm)
                role_counts[role] += 1
                made += 1
                items.append(
                    {
                        "id": f"{prefix}-{metric}-{made:02d}",
                        "slice": "answerable",
                        "category": metric,
                        "role": role,
                        "subject": emails[role],
                        "template_id": t.id,
                        "time_kind": spec["time_kind"],
                        "fewshot": t.fewshot,
                        "turns": [{"text": text, "expect": {"type": "answer"}}],
                        "gold_spec": full,
                    }
                )
                produced = True
                break
            stalls = 0 if produced else stalls + 1
    return items


def _handwritten_items(name: str, slice_name: str) -> list[dict]:
    emails = _emails()
    out = []
    for raw in load_handwritten(name):
        category = raw.get("category") or ("two_turn" if len(raw["turns"]) > 1 else "single_turn")
        if category in ("unapproved_dimension", "pii"):
            category = "unapproved_dimension_or_pii"
        item = {
            "id": raw["id"],
            "slice": slice_name,
            "category": category,
            "role": raw["role"],
            "subject": emails[raw["role"]],
            "template_id": None,
            "turns": raw["turns"],
        }
        if raw.get("gold_spec"):
            item["gold_spec"] = raw["gold_spec"]
        if raw.get("gold_specs"):
            item["gold_specs"] = raw["gold_specs"]
        out.append(item)
    return out


def expected_plan(spec: dict) -> dict:
    return {
        "action": "query",
        "metric": spec["metric"],
        "group_by": spec["group_by"],
        "grain": spec["grain"],
        "start": spec["start"],
        "end": spec["end"],
        "filters": [{"dimension": d, "values": v} for d, v in spec["filters"].items()],
        "clarification_question": "",
        "clarification_options": [],
        "refusal_reason": "",
    }


def generate() -> tuple[list[dict], list[dict]]:
    rng = random.Random(SEED)
    policy = load()
    seen: set[str] = set()
    eval_templates, _ = load_templates("answerable.yaml")
    dev_templates, dev_raw = load_templates("dev.yaml")
    eval_ids = {t.id for t in eval_templates}
    dev_ids = {t.id for t in dev_templates}
    if eval_ids & dev_ids or not all(i.startswith("T-") for i in eval_ids) or not all(i.startswith("D-") for i in dev_ids):
        raise RuntimeError("eval (T-*) and dev (D-*) template ids must be disjoint")

    # dev set first, so its normalized texts are reserved and eval items can never repeat them
    hand = dev_raw.get("handwritten") or []
    dev_per_metric = {m: 0 for m in policy.metrics}
    # The templated dev pass always draws DEV_TEMPLATE_ITEMS items so the shared random
    # stream (and therefore the frozen eval set) does not depend on how many handwritten
    # dev items exist; the extra templated items are dropped below.
    remaining = DEV_TEMPLATE_ITEMS
    for i, m in enumerate(policy.metrics):
        dev_per_metric[m] = remaining // len(policy.metrics) + (1 if i < remaining % len(policy.metrics) else 0)
    dev_items = _generate_from_templates(dev_templates, dev_per_metric, rng, "DEV", seen)
    keep = DEV_ITEMS - len(hand)
    if keep < len(dev_items):
        droppable = [i for i in reversed(range(len(dev_items))) if not dev_items[i].get("fewshot")]
        drop = set(droppable[: len(dev_items) - keep])
        dev_items = [item for i, item in enumerate(dev_items) if i not in drop]
    first_by_template: set[str] = set()
    for item in dev_items:
        item["slice"] = "dev"
        item["expected_plan"] = expected_plan(item["gold_spec"])
        is_first = item["template_id"] not in first_by_template
        first_by_template.add(item["template_id"])
        item["fewshot"] = bool(item.pop("fewshot")) and is_first
    emails = _emails()
    for h in hand:
        if "turns" in h:
            for turn in h["turns"]:
                seen.add(normalize_text(turn["text"]))
            dev_items.append(
                {
                    "id": h["id"],
                    "slice": "dev",
                    "category": "two_turn",
                    "role": h["role"],
                    "subject": emails[h["role"]],
                    "template_id": None,
                    "turns": h["turns"],
                    "gold_spec": h["gold_spec"],
                    "fewshot": False,
                }
            )
            continue
        seen.add(normalize_text(h["text"]))
        action = h["expected_plan"]["action"]
        dev_items.append(
            {
                "id": h["id"],
                "slice": "dev",
                "category": action,
                "role": h["role"],
                "subject": emails[h["role"]],
                "template_id": None,
                "turns": [{"text": h["text"], "expect": {"type": "refusal" if action == "refuse" else "clarification"}}],
                "expected_plan": h["expected_plan"],
                "fewshot": bool(h.get("fewshot")),
            }
        )

    for path in ("clarification.yaml", "adversarial.yaml"):
        for raw in load_handwritten(path):
            for turn in raw["turns"]:
                seen.add(normalize_text(turn["text"]))
    answerable = _generate_from_templates(eval_templates, {m: PER_METRIC for m in policy.metrics}, rng, "A", seen)
    for item in answerable:
        item.pop("fewshot", None)
    clarification = _handwritten_items("clarification.yaml", "clarification")
    adversarial = _handwritten_items("adversarial.yaml", "adversarial")
    return answerable + clarification + adversarial, dev_items


def dumps(items: list[dict]) -> str:
    return "".join(json.dumps(i, sort_keys=True) + "\n" for i in items)


def load_items(path=QUESTIONS_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write(check: bool = False) -> int:
    items, dev = generate()
    texts = {QUESTIONS_PATH: dumps(items), DEV_PATH: dumps(dev)}
    if check:
        bad = [p.name for p, t in texts.items() if not p.exists() or p.read_text(encoding="utf-8") != t]
        if bad:
            print(f"DRIFT: {', '.join(bad)} differ from a fresh generation")
            return 1
        print("OK eval and dev question files match a fresh generation")
        return 0
    for p, t in texts.items():
        p.write_text(t, encoding="utf-8")
    counts = Counter(i["slice"] for i in items)
    turns = sum(len(i["turns"]) for i in items)
    print(json.dumps({"items": len(items), "turns": turns, "slices": counts, "dev_items": len(dev)}))
    return 0
