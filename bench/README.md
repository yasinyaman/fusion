# Does the semantic layer earn its place?

A paired benchmark over one banking-shaped schema, 40 questions, half Turkish
and half English. Every arm answers the same question and returns the same
shape, so the runner cannot tell which is which. Grading is by **result set**
against a gold query, never by SQL text, because two correct queries rarely
look alike.

```bash
python -m bench.run_duckdb qwen2.5-coder:7b     # six arms on DuckDB, into bench/results/
pytest bench/tests                              # the harness itself
```

The two runs below are checked in under `bench/results-1.5b/` and
`bench/results-7b/`, named for the model that produced them; a fresh run
writes to `bench/results/`, which is ignored.

The thresholds that decide the outcome were **written down before the first
run** and are applied mechanically by `runner.verdict`, so the result is not
re-litigated after the fact.

## The arms

| Arm | How the request is chosen |
|---|---|
| `raw` | The model writes SQL from the DDL alone. |
| `catalog` | The model writes SQL from Warp's enriched `x-llm-context`. |
| `semantic` | The model writes a DSL request as free JSON. |
| `semantic-schema` | The same, with decoding constrained to a schema built from the semantic model. |
| `lexicon` | No model at all: words are matched against the semantic model. |
| `lexicon+repair` | The lexicon proposes; the model corrects it under the same constraint. |

## Results

Execution accuracy, 40 questions / the 34 that need no join. Six of the
questions require a join, which the DSL refuses by design in v1, so both
numbers are reported rather than averaged.

**qwen2.5-coder:1.5b**

| Arm | 40q | single-table | Hallucinated | Median latency |
|---|---|---|---|---|
| raw | 22.5% | 26.5% | 27.5% | 497 ms |
| catalog | 47.5% | 50.0% | 15.0% | 483 ms |
| semantic | 15.0% | 17.6% | 15.0% | 1404 ms |
| lexicon | 45.0% | 52.9% | **0%** | **2 ms** |
| semantic-schema | 32.5% | 38.2% | **0%** | 1660 ms |

**qwen2.5-coder:7b**

| Arm | 40q | single-table | Hallucinated | Median latency |
|---|---|---|---|---|
| raw | 45.0% | 41.2% | 32.5% | 8949 ms |
| catalog | 75.0% | 70.6% | 27.5% | 7347 ms |
| semantic | 37.5% | 44.1% | 2.5% | 8667 ms |
| lexicon | 47.5% | 55.9% | **0%** | **3 ms** |
| semantic-schema | 37.5% | 44.1% | **0%** | 16116 ms |
| **lexicon+repair** | **62.5%** | **73.5%** | **0%** | ~16 s |

## What the runs established

**Model size was the dominant variable, and testing it argued against the
DSL rather than for it.** Going from 1.5b to 7b gained the catalog arm 27.5
points and the DSL arm 5.0. The SQL arms convert model capacity into accuracy;
the DSL arm barely does. `semantic-schema vs catalog` is −37.5 points at
**p = 0.00006**, where the same comparison at 1.5b was p = 0.146. The
pre-registered verdict — stop funding the DSL through to joins — fires on the
larger model with far more force than on the smaller one.

**Constraining decoding removes form errors completely, and only matters
while they exist.** The unconstrained semantic arm failed 17 of 40 questions
on form alone: 7 metrics written `sum(tutar)` instead of `tutar:sum`, 6 names
that did not exist, 4 `order_by` values handed over as lists. Deriving a JSON
schema from the semantic model — one `oneOf` branch per table, one per
measure, values enumerated for categorical dimensions — made every one of
those unrepresentable, worth +17.5 points at 1.5b (p = 0.039). At 7b the same
constraint is worth nothing on accuracy (gap 0.0, p = 1), because a 7b model
does not make form errors. It still takes hallucination to zero, and it still
costs roughly double the latency.

**Instructions hurt a small model; constraints help it.** An earlier version
of the constrained prompt spelled out the pragmatics a schema cannot carry —
use a dimension for every "per X", a filter for every condition, `cumsum` for
a running total. It made the arm *worse*: 13/40 correct with two sentences
against 10/40 with the list, and one runtime failure against seven. Constrain
the output; do not lecture the model.

**A lexicon is a serious baseline, not a straw man.** `lexicon.py` matches
folded words against the semantic model — Turkish `başarılı` against the
stored `basarili` — and beat the 1.5b model outright. It answers in 3 ms, can
never name something that does not exist, and returns the same number on every
run. What it cannot do is generalise: its vocabulary is per schema and per
language.

**A model can write that vocabulary, which is where the model belongs.**
`lexicon_gen.py` spends one completion on the *schema* rather than one per
question, and the result is a JSON file: reviewable before use, diffable in
version control, correctable by whoever owns the data, and absent at query
time. Generation is itself constrained to the names that exist, so it can say
what a column is called but never invent one. The generated lexicon scored
47.5% against the hand-written 45.0%, from a single 49-second call.

**Deterministic failures are near-misses; model failures are not.** Of the
lexicon's 21 wrong answers, 11 were a single field away from right — it drops
one condition because it had no word for it. Of `semantic-schema`'s 25, only
2 were. A copilot lives on that difference: correct one field, or start again.
`repair.py` measures it, and the same neighbourhood is a ready-made "did you
mean" menu — which can be computed for a structured request and cannot be
computed for a SELECT statement.

**Seeding the model with the deterministic answer beat everything.**
`lexicon+repair` reached 73.5% on single-table questions with zero
hallucination, above the catalog arm's 70.6% with 27.5%. It fixed 6 of the
lexicon's wrong answers and **broke none of the 19 it had right**. Each part
does what it is good at: word matching finds the columns and values, the model
judges whether that is what was asked, the schema stops either from inventing
anything.

**That design is sharply model-size dependent.** The same arm on 1.5b broke
**11 of the 19** proposals the lexicon had right — net −7. A small model
vandalises a good deterministic answer. `RepairArm(fall_back=True)` refuses a
correction that will not run, which is the right default for a product but was
left off in the measured arm so the numbers report what the model actually did.

## Caveats

- **N = 40**, over a 10-row fixture. Confidence intervals are wide and mostly
  overlapping; the only comparisons that separate cleanly are
  `semantic-schema` against `catalog` and against unconstrained `semantic`.
- **The lexicon was corrected three times against this question set; the LLM
  arms were not.** Its first, untuned score was 15/40. Read its number as
  optimistic in a way the others' are not.
- **One-edit repair is judged on gold rows**, so on a small fixture a
  coincidentally matching neighbour counts as a repair.
- **Hallucination rises with model size on the SQL arms** (catalog 15% → 27.5%)
  while only one query failed to run at 7b — so the flag there is mostly
  aliasing, not broken SQL, and should not be read as it is at 1.5b.
- **Oracle and SQL Server arms are wired and unrun.** `seed_oracle.sql` and
  `models.oracle_executor` are in place; the run needs a container.

## Layout

| File | |
|---|---|
| `arms.py` | The arms, and the one execution path they share. |
| `constrain.py` | The semantic model as a decoding constraint. |
| `lexicon.py` | Word matching, with no model in it. |
| `lexicon_gen.py` | A model writing the lexicon, once per schema. |
| `repair.py` | One-edit repair: how much work a wrong answer leaves. |
| `scoring.py`, `stats.py` | Grading, Wilson intervals, McNemar exact. |
| `runner.py` | Running arms, pairing them, applying the thresholds. |
| `questions/` | The question set, as data. |
| `lexicons/` | Generated vocabularies, checked in for review. |
