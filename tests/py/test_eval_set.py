"""The frozen eval set: exactly 200 items (150/20/30), 214 turns, disjoint dev set."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from peoplelens.eval import generate
from peoplelens.eval.gold import GOLD_PATH, build, dump
from peoplelens.eval.templates import normalize_text
from peoplelens.paths import GENERATED_DIR
from peoplelens.policy import load

ITEMS = generate.load_items(generate.QUESTIONS_PATH)
DEV = generate.load_items(generate.DEV_PATH)


def test_exact_counts():
    assert len(ITEMS) == 200
    assert Counter(i["slice"] for i in ITEMS) == {"answerable": 150, "clarification": 20, "adversarial": 30}
    assert sum(len(i["turns"]) for i in ITEMS) == 214
    answerable = [i for i in ITEMS if i["slice"] == "answerable"]
    assert Counter(i["category"] for i in answerable) == {m: 25 for m in load().metrics}
    clar = [i for i in ITEMS if i["slice"] == "clarification"]
    assert Counter(len(i["turns"]) for i in clar) == {1: 10, 2: 10}
    adv = [i for i in ITEMS if i["slice"] == "adversarial"]
    assert Counter(i["category"] for i in adv) == generate.ADVERSARIAL_CATEGORIES
    assert sum(len(i["turns"]) for i in adv) == 34
    assert Counter(len(i["turns"]) for i in adv if i["category"] == "differencing") == {2: 4}
    assert len({i["id"] for i in ITEMS}) == 200


def test_dev_set_is_disjoint():
    assert len(DEV) == 60
    eval_templates = {i["template_id"] for i in ITEMS if i["template_id"]}
    dev_templates = {i["template_id"] for i in DEV if i["template_id"]}
    assert all(t.startswith("T-") for t in eval_templates)
    assert all(t.startswith("D-") for t in dev_templates)
    assert not eval_templates & dev_templates
    eval_texts = {normalize_text(t["text"]) for i in ITEMS for t in i["turns"]}
    dev_texts = {normalize_text(t["text"]) for i in DEV for t in i["turns"]}
    assert not eval_texts & dev_texts


def test_fewshot_examples_come_only_from_the_dev_set():
    fewshot = json.loads((GENERATED_DIR / "fewshot.json").read_text())
    assert len(fewshot) == 12
    dev_ids = {i["id"] for i in DEV}
    eval_texts = {normalize_text(t["text"]) for i in ITEMS for t in i["turns"]}
    for ex in fewshot:
        assert ex["id"] in dev_ids
        assert normalize_text(ex["question"]) not in eval_texts
    actions = Counter(ex["output"]["action"] for ex in fewshot)
    assert actions["query"] >= 8 and actions["clarify"] >= 1 and actions["refuse"] >= 1


def test_answerable_items_are_permitted_and_fully_visible():
    k = load().limits["min_cohort"]
    gold = json.loads(GOLD_PATH.read_text())
    for item in ITEMS:
        if item["slice"] != "answerable":
            continue
        spec = item["gold_spec"]
        assert item["role"] in generate.permitted_roles(spec), item["id"]
        if item["role"] == "hr_business_partner":
            assert spec["scope"] == ["Engineering", "Sales"]
            assert set(spec["filters"].get("department", [])) <= {"Engineering", "Sales"}
        cells = gold[item["id"]]["cells"]
        assert cells, item["id"]
        assert all(c["cohort"] >= k and not c["expected_suppressed"] for c in cells), item["id"]


def test_two_turn_and_probe_gold():
    gold = json.loads(GOLD_PATH.read_text())
    for item in ITEMS:
        if item["slice"] == "clarification" and len(item["turns"]) == 2:
            assert item["id"] in gold
            assert all(not c["expected_suppressed"] for c in gold[item["id"]]["cells"])
    assert gold["AD-SC-01#0"]["cells"][0]["expected_suppressed"] is True
    assert gold["AD-DF-01#1"]["cells"][0]["suppression_reason"] == "complement"


def test_frozen_files_match_a_fresh_generation():
    items, dev = generate.generate()
    assert generate.dumps(items) == generate.QUESTIONS_PATH.read_text()
    assert generate.dumps(dev) == generate.DEV_PATH.read_text()
    assert dump(build(ITEMS)) == GOLD_PATH.read_text()
