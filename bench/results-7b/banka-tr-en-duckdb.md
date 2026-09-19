# Benchmark: banka-tr-en on duckdb

40 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 45.0% [30.7%, 60.2%] | 32.5% | 10.0% | 2864 ms |
| catalog | 75.0% [59.8%, 85.8%] | 27.5% | 15.0% | 3726 ms |
| semantic | 37.5% [24.2%, 53.0%] | 2.5% | 0.0% | 4733 ms |
| lexicon | 52.5% [37.5%, 67.1%] | 0.0% | 0.0% | 3 ms |
| semantic-schema | 40.0% [26.3%, 55.4%] | 0.0% | 0.0% | 8365 ms |
| lexicon+repair | 70.0% [54.6%, 81.9%] | 0.0% | 0.0% | 7909 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| lexicon+repair vs raw | +25.0 pts | 16 | 6 | 0.05248 |
| lexicon+repair vs catalog | -5.0 pts | 6 | 8 | 0.7905 |
| lexicon+repair vs semantic | +32.5 pts | 13 | 0 | 0.0002441 |
| lexicon+repair vs lexicon | +17.5 pts | 7 | 0 | 0.01562 |
| lexicon+repair vs semantic-schema | +30.0 pts | 12 | 0 | 0.0004883 |

## Verdict

Stop investing in the DSL: it beats the catalog arm by only -5.0 points (< 5.0). Put the effort into catalog quality and text-to-SQL.

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.

## Where each arm's answers went

| Outcome | raw | catalog | semantic | lexicon | semantic-schema | lexicon+repair |
|---|---|---|---|---|---|---|
| correct | 18 | 30 | 15 | 21 | 16 | 28 |
| named something that does not exist | 0 | 0 | 5 | 0 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 2 | 1 |
| ran, but answered the wrong question | 21 | 9 | 17 | 19 | 22 | 11 |
| the language was written wrongly | 0 | 0 | 3 | 0 | 0 | 0 |
| the query would not run | 1 | 1 | 0 | 0 | 0 | 0 |

"Ran, but answered the wrong question" is the important row: the layer did its job and the model asked for the wrong thing. That is a case for a better model, not a different layer.
