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

The thresholds that decide the outcome are applied mechanically by
`runner.verdict`, so the result is not re-litigated after the fact. They were
fixed before the first run, but the repository cannot show that: thresholds,
arms and results all arrive in the same commit (`6eae338`), and the arm the
verdict judges was chosen after the others had been tried.

## The arms

| Arm | How the request is chosen |
|---|---|
| `raw` | The model writes SQL from the DDL alone. |
| `catalog` | The model writes SQL from a catalog-style description of the schema — hand-written in `run_duckdb.py` in the shape of Warp's `x-llm-context`, not fetched from Warp. |
| `semantic` | The model writes a DSL request as free JSON. |
| `semantic-schema` | The same, with decoding constrained to a schema built from the semantic model. |
| `lexicon` | No model at all: words are matched against the semantic model. |
| `lexicon+repair` | The lexicon proposes; the model corrects it under the same constraint. |

## Results

Execution accuracy, 40 questions / the 34 that need no join. Six of the
questions require a join, which the DSL refuses by design in v1, so both
numbers are reported rather than averaged.

> **Provenance.** Every number below was produced at commit `ddc1d3a`. The
> commit after it, `28f2574`, changed `repair.py`, `arms.py`, `lexicon.py`,
> `runner.py` and `scoring.py` in response to a review, and the run was **not**
> repeated — so the `lexicon+repair` row in particular has not been re-measured
> against the code now in the tree. Reproduce with
> `python -m bench.run_duckdb qwen2.5-coder:7b bench/results-7b` (~30 min,
> needs a local ollama) before quoting these anywhere that matters.

> **The "Hallucinated" column is not comparable between the SQL arms and the
> rest.** For `raw` and `catalog` it was produced by a word-based check that
> also counted table aliases, CTE names and ordinary functions as invented
> names — see the caveats. The numbers are left as measured.

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
7 metrics written with no aggregation (`count`, `*`), 6 names used on a table
that does not have them (5 of them `segment` on `islemler`, in join questions
the DSL cannot answer anyway), 4 `order_by` values handed over as lists or
objects. A JSON schema derived from the
semantic model — one `oneOf` branch per table, one per measure, values
enumerated for categorical dimensions — made every one of those
unrepresentable. The constrained arm scores +17.5 points at 1.5b and +2.5 at
7b, where the model does not make those mistakes — but that gap is not the
constraint alone: the constrained prompt also lists every categorical value
(`durum (basarili | iptal)`), which the unconstrained arm never sees, and uses
a different instruction with no worked example. It still cannot name something
that does not exist, and it still costs roughly double the latency.

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
21/40 against the hand-written 20/40, from a single 37-second call. That is
weaker evidence than it looks: the generation prompt's two worked examples are
the hand-written entries for `tutar` and `basarili` — the measure and the
filter value most of these questions turn on — so the model was shown the
answers that matter most. The matcher's aggregation and grain tables are
hand-written for both.

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

**Seeding the model with the deterministic answer gave the best DSL arm, and
did not beat the catalog arm.** `lexicon+repair` reached 82.4% on single-table
questions against the catalog arm's 70.6%, a gap this run cannot tell from
chance (3 questions against 1, p = 0.63). Against the lexicon alone it is +20.6
points with 7 answers fixed and none broken — but those are 4 distinct
questions, each asked in two languages, so the evidence is 4 against 0
(p = 0.125), not 7 against 0. Against the unconstrained and the constrained
DSL arms it is 8 questions against 0 (p = 0.008). Each part does what it is
good at: word matching finds the columns and values, the model
judges whether that is what was asked, the schema stops either from inventing
anything.

**And it is sharply model-size dependent — measured, not assumed.** The same
arm on 1.5b scores 27.5% where the lexicon alone scores 52.5%: it fixed 1
answer and **broke 11** — 7 distinct questions against 1, p = 0.07. A small
model does not correct a good deterministic answer, it vandalises it. `RepairArm(fall_back=True)` keeps the
proposal when a correction will not run, and `Answer.fell_back` records that it
did — but nothing stops a model from confidently replacing a right answer with
a wrong one, so below some capacity this design is worse than no model at all.

## Caveats

- **N = 20, asked twice.** The 40 items are 20 questions in Turkish and in
  English, over a 10-row fixture, and an arm almost always gets both languages
  of a question right or both wrong (20 of 20 for `lexicon+repair` at 7b). The
  per-item McNemar in the result files treats them as 40 independent pairs and
  so overstates the evidence. The p-values quoted above count each question
  once (`stats.sign_test_by_cluster`; `tests/test_readme_claims.py` recomputes
  them from the checked-in results). On that count only `lexicon+repair`
  against `semantic` and against `semantic-schema` at 7b fall below 0.05
  (p = 0.008 each), and with five comparisons on two question sets and two
  models not even those survive a correction for multiple comparisons.
  `lexicon+repair` against `lexicon` is suggestive in both directions and
  significant in neither; against `catalog` it is a tie.
- **Grading favours answers shaped like the DSL's.** A result must match the
  gold rows column for column, in the gold's column order, and the gold queries
  project the way the DSL does — dimension first, month as a midnight
  timestamp. A correct SQL answer with the columns swapped, with `SELECT *`, or
  with the month as a date or `'2024-01'` is graded wrong. `q07` and `q15` were
  answered correctly by no arm in either run. How many SQL-arm losses this
  caused cannot be recovered: the result files keep no answer text.
- **The lexicon was corrected against this question set; the LLM arms were
  not.** Its first untuned score was 15/40. Read its number as optimistic in a
  way the others' are not.
- **One-edit repair is judged on gold rows**, so on a small fixture a
  coincidentally matching neighbour counts as a repair.
- **The SQL arms' hallucination rate is an artefact at 7b.** The check that
  produced it read identifier-looking *words* out of the SQL. At 7b every name
  it flagged for `raw` and `catalog` is a table alias (`i`, `m`, `t1`, `i2`), a
  keyword (`within`, `month`, `year`, `current_date`) or a function
  (`percentile_cont`, `date_sub`); 10 of the 24 flagged answers were graded
  correct and the one query that failed in each arm failed on a function
  signature, not a missing column. So 32.5% and 27.5% are not rates of invented
  names, and no comparison of hallucination between the SQL arms and the DSL
  arms should be drawn from these runs. At 1.5b some flags are real (`canal`,
  `channel`, `amount`). The same check also *missed* a real column used on the
  wrong table. `scoring.py` now reads names from the parsed statement, which
  fixes the false positives for the next run; these runs cannot be re-scored
  because the answers were not kept. That the DSL arms cannot name a missing
  column is structural, and does not depend on this column.
- **The raw arm is told to write DuckDB SQL and shown Oracle DDL**
  (`NUMBER`, `VARCHAR2`), while the catalog arm gets a dialect-neutral
  description with English glosses. Catalog-over-raw therefore mixes what a
  catalog adds with what a mismatched dialect costs.
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
| `scoring.py`, `stats.py` | Grading, Wilson intervals, McNemar exact, and the same test with one vote per question. |
| `runner.py` | Running arms, pairing them, applying the thresholds. |
| `questions/` | The question set, as data. |
| `lexicons/` | Generated vocabularies, checked in for review. |
