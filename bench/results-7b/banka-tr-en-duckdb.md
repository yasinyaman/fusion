# Benchmark: banka-tr-en on duckdb

40 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 45.0% [30.7%, 60.2%] | 32.5% | 10.0% | 8949 ms |
| catalog | 75.0% [59.8%, 85.8%] | 27.5% | 15.0% | 7347 ms |
| semantic | 37.5% [24.2%, 53.0%] | 2.5% | 0.0% | 8667 ms |
| lexicon | 45.0% [30.7%, 60.2%] | 0.0% | 0.0% | 3 ms |
| semantic-schema | 37.5% [24.2%, 53.0%] | 0.0% | 0.0% | 16116 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| semantic-schema vs raw | -7.5 pts | 4 | 7 | 0.5488 |
| semantic-schema vs catalog | -37.5 pts | 0 | 15 | 6.104e-05 |
| semantic-schema vs semantic | +0.0 pts | 4 | 4 | 1 |
| semantic-schema vs lexicon | -7.5 pts | 4 | 7 | 0.5488 |

## Verdict

Stop investing in the DSL: it beats the catalog arm by only -37.5 points (< 5.0). Put the effort into catalog quality and text-to-SQL.

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.

## Where each arm's answers went

| Outcome | raw | catalog | semantic | lexicon | semantic-schema |
|---|---|---|---|---|---|
| correct | 18 | 30 | 15 | 18 | 15 |
| named something that does not exist | 0 | 0 | 5 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 3 |
| ran, but answered the wrong question | 21 | 9 | 17 | 22 | 22 |
| the language was written wrongly | 0 | 0 | 3 | 0 | 0 |
| the query would not run | 1 | 1 | 0 | 0 | 0 |

"Ran, but answered the wrong question" is the important row: the layer did its job and the model asked for the wrong thing. That is a case for a better model, not a different layer.
