# Benchmark: banka-tr-en (single-table only) on duckdb

34 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 26.5% [14.6%, 43.1%] | 32.4% | 23.5% | 475 ms |
| catalog | 50.0% [34.1%, 65.9%] | 14.7% | 41.2% | 475 ms |
| semantic | 17.6% [8.3%, 33.5%] | 17.6% | 0.0% | 1456 ms |
| lexicon | 61.8% [45.0%, 76.1%] | 0.0% | 0.0% | 2 ms |
| semantic-schema | 38.2% [23.9%, 55.0%] | 0.0% | 0.0% | 1615 ms |
| lexicon+repair | 32.4% [19.1%, 49.2%] | 0.0% | 0.0% | 2059 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| lexicon+repair vs raw | +5.9 pts | 5 | 3 | 0.7266 |
| lexicon+repair vs catalog | -17.6 pts | 4 | 10 | 0.1796 |
| lexicon+repair vs semantic | +14.7 pts | 7 | 2 | 0.1797 |
| lexicon+repair vs lexicon | -29.4 pts | 1 | 11 | 0.006348 |
| lexicon+repair vs semantic-schema | -5.9 pts | 2 | 4 | 0.6875 |

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.