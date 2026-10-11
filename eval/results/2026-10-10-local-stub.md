# PeopleLens eval: local, stub (stub)

- Date: 2026-10-10
- Command: `uv run pl-eval run --llm stub --assert-safety --official`
- Commit: `0bc320ae7c251675d5204a5fd3ae63e734a124e9`; official: True
- Warehouse: duckdb_local; hardware: Darwin 25.5.0 arm64, Apple M5
- Items: 200 (adversarial 30, answerable 150, clarification 20); turns: 214
- Duration: 128.3 s (pacing waits 114.7 s, never counted as latency)
- Machine load average (1, 5, 15 min) at start and end: [47.3, 35.3, 31.2] / [8.7, 24.2, 27.3]

## Semantic path (the product)

| Metric | Value |
|---|---|
| Result accuracy | not reported (stub planner (CI fixture); accuracy is reported only for real models) |
| Answers on the answerable slice | 148 of 150 |
| Adversarial pass rate (30) | 100.0% (30/30) |
| Safety violations | 0 |
| p95 latency, answerable final turns | 40.8 ms |
| Latency over all 214 turns (p50 / p95 / p99 / max) | 15.9 / 37.2 / 82.7 / 1635.5 ms |
| Server LLM time (p50 / p95) | 0 / 0 ms |
| Server warehouse time (p50 / p95) | 5.5 / 13.4 ms |
| Meets 90% accuracy target | not reported |
| Meets p95 < 5 s target | True |

Accuracy by metric: not reported

Response types: answer 172, clarification 22, refusal 20

Refusal codes: DIMENSION_NOT_APPROVED 5, METRIC_NOT_ALLOWED 1, PII_REQUEST 5, RESULT_TOO_LARGE 3, SCOPE_VIOLATION 3, WRITE_REQUEST 3

Text-to-SQL baseline: not run for this target.
