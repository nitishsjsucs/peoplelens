# PeopleLens eval: local, stub (stub)

- Date: 2026-10-10
- Command: `uv run pl-eval run --llm stub --assert-safety --official`
- Commit: `c0271ce209295f859f2c6cbc4db49158ffe83119`; official: True
- Warehouse: duckdb_local; hardware: Darwin 25.5.0 arm64, Apple M5
- Items: 200 (adversarial 30, answerable 150, clarification 20); turns: 214
- Duration: 126.8 s (pacing waits 114.7 s, never counted as latency)

## Semantic path (the product)

| Metric | Value |
|---|---|
| Result accuracy | not reported (stub planner (CI fixture); accuracy is reported only for real models) |
| Answers on the answerable slice | 148 of 150 |
| Adversarial pass rate (30) | 100.0% (30/30) |
| Safety violations | 0 |
| p95 latency, answerable final turns | 30.3 ms |
| Latency over all 214 turns (p50 / p95 / p99 / max) | 15.9 / 29.7 / 36.1 / 1690.8 ms |
| Server LLM time (p50 / p95) | 0 / 0 ms |
| Server warehouse time (p50 / p95) | 5.8 / 10.5 ms |
| Meets 90% accuracy target | not reported |
| Meets p95 < 5 s target | True |

Accuracy by metric: not reported

Response types: answer 172, clarification 22, refusal 20

Refusal codes: DIMENSION_NOT_APPROVED 5, METRIC_NOT_ALLOWED 1, PII_REQUEST 5, RESULT_TOO_LARGE 3, SCOPE_VIOLATION 3, WRITE_REQUEST 3

Text-to-SQL baseline: not run for this target.
