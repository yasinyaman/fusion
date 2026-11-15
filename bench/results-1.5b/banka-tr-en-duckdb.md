# Benchmark: banka-tr-en on duckdb

40 questions answered by every arm.

| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | Median latency |
|---|---|---|---|---|
| raw | 22.5% [12.3%, 37.5%] | 27.5% | 20.0% | 482 ms |
| catalog | 47.5% [32.9%, 62.5%] | 15.0% | 35.0% | 474 ms |
| semantic | 15.0% [7.1%, 29.1%] | 15.0% | 0.0% | 1400 ms |
| lexicon | 52.5% [37.5%, 67.1%] | 0.0% | 0.0% | 2 ms |
| semantic-schema | 32.5% [20.1%, 48.0%] | 0.0% | 0.0% | 1748 ms |
| lexicon+repair | 27.5% [16.1%, 42.8%] | 0.0% | 0.0% | 2039 ms |

## Paired comparisons (McNemar exact)

| Comparison | Gap | Only first | Only second | p |
|---|---|---|---|---|
| lexicon+repair vs raw | +5.0 pts | 5 | 3 | 0.7266 |
| lexicon+repair vs catalog | -20.0 pts | 4 | 12 | 0.07681 |
| lexicon+repair vs semantic | +12.5 pts | 7 | 2 | 0.1797 |
| lexicon+repair vs lexicon | -25.0 pts | 1 | 11 | 0.006348 |
| lexicon+repair vs semantic-schema | -5.0 pts | 2 | 4 | 0.6875 |

## Verdict

Stop investing in the DSL: it beats the catalog arm by only -20.0 points (< 5.0). Put the effort into catalog quality and text-to-SQL.

Exact match is a diagnostic, not a score: a low exact-match with a high execution accuracy is the expected, healthy pattern, because two correct queries rarely look alike.

## Where each arm's answers went

| Outcome | raw | catalog | semantic | lexicon | semantic-schema | lexicon+repair |
|---|---|---|---|---|---|---|
| correct | 9 | 19 | 6 | 21 | 13 | 11 |
| named something that does not exist | 0 | 0 | 6 | 0 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 1 | 1 |
| ran, but answered the wrong question | 24 | 13 | 17 | 19 | 26 | 28 |
| the language was written wrongly | 0 | 0 | 11 | 0 | 0 | 0 |
| the query would not run | 7 | 8 | 0 | 0 | 0 | 0 |

"Ran, but answered the wrong question" is the important row: the layer did its job and the model asked for the wrong thing. That is a case for a better model, not a different layer.
