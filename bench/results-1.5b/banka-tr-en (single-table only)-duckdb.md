# Benchmark: banka-tr-en (single-table only) on duckdb

34 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 26.5% [14.6%, 43.1%] | 32.4% | 23.5% | 488 ms |
| catalog | 50.0% [34.1%, 65.9%] | 14.7% | 41.2% | 484 ms |
| semantic | 17.6% [8.3%, 33.5%] | 17.6% | 0.0% | 1462 ms |
| lexicon | 52.9% [36.7%, 68.5%] | 0.0% | 0.0% | 2 ms |
| semantic-schema | 38.2% [23.9%, 55.0%] | 0.0% | 0.0% | 1582 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| semantic-schema vs raw | +11.8 pts | 7 | 3 | 0.3438 |
| semantic-schema vs catalog | -11.8 pts | 3 | 7 | 0.3438 |
| semantic-schema vs semantic | +20.6 pts | 8 | 1 | 0.03906 |
| semantic-schema vs lexicon | -14.7 pts | 4 | 9 | 0.2668 |

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.