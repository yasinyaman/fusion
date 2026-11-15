# Benchmark: banka-tr-en (single-table only) on duckdb

34 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 41.2% [26.4%, 57.8%] | 20.6% | 11.8% | 2632 ms |
| catalog | 70.6% [53.8%, 83.2%] | 20.6% | 17.6% | 3425 ms |
| semantic | 44.1% [28.9%, 60.5%] | 2.9% | 0.0% | 4770 ms |
| lexicon | 61.8% [45.0%, 76.1%] | 0.0% | 0.0% | 2 ms |
| semantic-schema | 47.1% [31.5%, 63.3%] | 0.0% | 0.0% | 8127 ms |
| lexicon+repair | 82.4% [66.5%, 91.7%] | 0.0% | 0.0% | 7799 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| lexicon+repair vs raw | +41.2 pts | 16 | 2 | 0.001312 |
| lexicon+repair vs catalog | +11.8 pts | 6 | 2 | 0.2891 |
| lexicon+repair vs semantic | +38.2 pts | 13 | 0 | 0.0002441 |
| lexicon+repair vs lexicon | +20.6 pts | 7 | 0 | 0.01562 |
| lexicon+repair vs semantic-schema | +35.3 pts | 12 | 0 | 0.0004883 |

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.