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
| raw | 22.5% | 26.5% | 27.5% | 482 ms |
| catalog | 47.5% | 50.0% | 15.0% | 474 ms |
| semantic | 15.0% | 17.6% | 15.0% | 1400 ms |
| **lexicon** | **52.5%** | **61.8%** | **0%** | **2 ms** |
| semantic-schema | 32.5% | 38.2% | **0%** | 1748 ms |
| lexicon+repair | 27.5% | 32.4% | **0%** | 2039 ms |

**qwen2.5-coder:7b**

| Arm | 40q | single-table | Hallucinated | Median latency |
|---|---|---|---|---|
| raw | 45.0% | 41.2% | 32.5% | 2864 ms |
| catalog | 75.0% | 70.6% | 27.5% | 3726 ms |
| semantic | 37.5% | 44.1% | 2.5% | 4733 ms |
| lexicon | 52.5% | 61.8% | **0%** | **3 ms** |
| semantic-schema | 40.0% | 47.1% | **0%** | 8365 ms |
| **lexicon+repair** | **70.0%** | **82.4%** | **0%** | 7909 ms |

## What the runs established

**Model size is the dominant variable, and it argues against the DSL on its
own.** From 1.5b to 7b the catalog arm gained 27.5 points and the constrained
DSL arm 7.5. The SQL arms convert model capacity into accuracy; the DSL arm
barely does. The pre-registered verdict fires on the full question set —
`lexicon+repair` trails `catalog` by 5.0 points there, because six of the forty
need a join the DSL refuses outright. On the 34 it can answer, that reverses to
+11.8, which is why both numbers are reported.

**Constraining decoding removes form errors completely, and only matters while
they exist.** The unconstrained semantic arm failed 17 of 40 on form alone:
7 metrics written `sum(tutar)` instead of `tutar:sum`, 6 names that did not
exist, 4 `order_by` values handed over as lists. A JSON schema derived from the
semantic model — one `oneOf` branch per table, one per measure, values
enumerated for categorical dimensions — made every one of those
unrepresentable, worth +17.5 points at 1.5b. At 7b it is worth +2.5, because a
7b model does not make those mistakes. It still takes hallucination to zero,
and it still costs roughly double the latency.

**Instructions hurt a small model; constraints help it.** An earlier version of
the constrained prompt spelled out the pragmatics a schema cannot carry — use a
dimension for every "per X", a filter for every condition, `cumsum` for a
running total. It made the arm *worse*: 13/40 correct with two sentences
against 10/40 with the list, and one runtime failure against seven. Constrain
the output; do not lecture the model.

**A lexicon is a serious baseline, not a straw man.** `lexicon.py` matches
folded words against the semantic model — Turkish `başarılı` against the stored
`basarili` — and beats every 1.5b arm outright. It answers in 2 ms, can never
name something that does not exist, and returns the same number on every run:
52.5% in both tables above, which is what "deterministic" looks like in a
results table. What it cannot do is generalise — its vocabulary is per schema
and per language.

**A model can write that vocabulary, which is where the model belongs.**
`lexicon_gen.py` spends one completion on the *schema* rather than one per
question, and the result is a JSON file: reviewable before use, diffable in
version control, correctable by whoever owns the data, and absent at query
time. Generation is itself constrained to the names that exist, so it can say
what a column is called but never invent one. The generated lexicon scores
21/40 against the hand-written 20/40, from a single 37-second call.

Getting that schema right took two tries, in opposite directions. Requiring a
word for every name produced `tutar: ["tutar", " tutar"]` — with no way to say
"nothing to add", the model filled the array with inflections of the name.
Dropping the requirement made it skip `segment` and `sube_kodu` entirely rather
than consider them. Every name is now required, and allowed to be empty.

**Deterministic failures are near-misses; model failures are not.** Of the
lexicon's wrong answers, roughly half are a single field from right — it drops
one condition because it had no word for it. Of `semantic-schema`'s, almost
none are. A copilot lives on that difference: correct one field, or start
again. `repair.py` measures it, and the same neighbourhood is a ready-made "did
you mean" menu — computable for a structured request, not for a SELECT.

**Seeding the model with the deterministic answer beat everything.**
`lexicon+repair` reached 82.4% on single-table questions with zero
hallucination, against the catalog arm's 70.6% with 20.6%. Against the lexicon
alone it is +20.6 points with **7 questions fixed and none broken**
(p = 0.016); against the unconstrained DSL arm +38.2 (p = 0.0002). Each part
does what it is good at: word matching finds the columns and values, the model
judges whether that is what was asked, the schema stops either from inventing
anything.

**And it is sharply model-size dependent — measured, not assumed.** The same
arm on 1.5b scores 27.5% where the lexicon alone scores 52.5%: it fixed 1
question and **broke 11**, p = 0.006. A small model does not correct a good
deterministic answer, it vandalises it. `RepairArm(fall_back=True)` keeps the
proposal when a correction will not run, and `Answer.fell_back` records that it
did — but nothing stops a model from confidently replacing a right answer with
a wrong one, so below some capacity this design is worse than no model at all.

## Caveats

- **N = 40**, over a 10-row fixture. Confidence intervals are wide and mostly
  overlapping. The comparisons that separate cleanly are `lexicon+repair`
  against `lexicon` (in both directions, one run each), against `semantic` and
  against `semantic-schema`. `lexicon+repair` against `catalog` does **not**
  separate (p = 0.29 on the subset): read the accuracy gap as a tie and the
  hallucination gap as the real difference.
- **The lexicon was corrected against this question set; the LLM arms were
  not.** Its first untuned score was 15/40. Read its number as optimistic in a
  way the others' are not.
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
