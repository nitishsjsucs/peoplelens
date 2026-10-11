# PeopleLens eval: local, stub (stub)

- Date: 2026-10-11
- Command: `uv run pl-eval run --llm stub --assert-safety --official`
- Commit: `9cadf24af23aa5134bbcb4ddc4435d41483507e5`; official: True
- Warehouse: duckdb_local; hardware: Darwin 25.5.0 arm64, Apple M5
- Items: 200 (adversarial 30, answerable 150, clarification 20); turns: 214
- Duration: 123.9 s (pacing waits 117.3 s, never counted as latency)
- Machine load average (1, 5, 15 min) at start and end: [5.8, 4.6, 4.5] / [4.9, 4.7, 4.6]

## Semantic path (the product)

| Metric | Value |
|---|---|
| Result accuracy | not reported (stub planner (CI fixture); accuracy is reported only for real models) |
| Answers on the answerable slice | 148 of 150 |
| Adversarial pass rate (30) | 100.0% (30/30) |
| Safety violations | 0 |
| p95 latency, answerable final turns | 17.2 ms |
| Latency over all 214 turns (p50 / p95 / p99 / max) | 9.1 / 16.7 / 22.0 / 497.6 ms |
| Server LLM time (p50 / p95) | 0 / 0 ms |
| Server warehouse time (p50 / p95) | 3.7 / 7.6 ms |
| Meets 90% accuracy target | not reported |
| Meets p95 < 5 s target | True |

Accuracy by metric: not reported

Response types: answer 172, clarification 22, refusal 20

Refusal codes: DIMENSION_NOT_APPROVED 5, METRIC_NOT_ALLOWED 1, PII_REQUEST 5, RESULT_TOO_LARGE 3, SCOPE_VIOLATION 3, WRITE_REQUEST 3

Text-to-SQL baseline: not run for this target.
