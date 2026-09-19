# Benchmark: banka-tr-en (single-table only) on duckdb

34 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 41.2% [26.4%, 57.8%] | 20.6% | 11.8% | 8191 ms |
| catalog | 70.6% [53.8%, 83.2%] | 20.6% | 17.6% | 6667 ms |
| semantic | 44.1% [28.9%, 60.5%] | 2.9% | 0.0% | 8842 ms |
| lexicon | 52.9% [36.7%, 68.5%] | 0.0% | 0.0% | 3 ms |
| semantic-schema | 44.1% [28.9%, 60.5%] | 0.0% | 0.0% | 15930 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| semantic-schema vs raw | +2.9 pts | 4 | 3 | 1 |
| semantic-schema vs catalog | -26.5 pts | 0 | 9 | 0.003906 |
| semantic-schema vs semantic | +0.0 pts | 4 | 4 | 1 |
| semantic-schema vs lexicon | -8.8 pts | 4 | 7 | 0.5488 |

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.