# Benchmark: banka-tr-en on duckdb

40 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 22.5% [12.3%, 37.5%] | 27.5% | 20.0% | 497 ms |
| catalog | 47.5% [32.9%, 62.5%] | 15.0% | 35.0% | 483 ms |
| semantic | 15.0% [7.1%, 29.1%] | 15.0% | 0.0% | 1404 ms |
| lexicon | 45.0% [30.7%, 60.2%] | 0.0% | 0.0% | 2 ms |
| semantic-schema | 32.5% [20.1%, 48.0%] | 0.0% | 0.0% | 1660 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| semantic-schema vs raw | +10.0 pts | 7 | 3 | 0.3438 |
| semantic-schema vs catalog | -15.0 pts | 3 | 9 | 0.146 |
| semantic-schema vs semantic | +17.5 pts | 8 | 1 | 0.03906 |
| semantic-schema vs lexicon | -12.5 pts | 4 | 9 | 0.2668 |

## Verdict

Stop investing in the DSL: it beats the catalog arm by only -15.0 points (< 5.0). Put the effort into catalog quality and text-to-SQL.

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.

## Where each arm's answers went

| Outcome | raw | catalog | semantic | lexicon | semantic-schema |
|---|---|---|---|---|---|
| correct | 9 | 19 | 6 | 18 | 13 |
| named something that does not exist | 0 | 0 | 6 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 1 |
| ran, but answered the wrong question | 24 | 13 | 17 | 22 | 26 |
| the language was written wrongly | 0 | 0 | 11 | 0 | 0 |
| the query would not run | 7 | 8 | 0 | 0 | 0 |

"Ran, but answered the wrong question" is the important row: the layer did its job and the model asked for the wrong thing. That is a case for a better model, not a different layer.
