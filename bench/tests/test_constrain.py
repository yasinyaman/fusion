"""The decoding constraint, and the property that makes it worth having.

The point of this module is not that the schema is well-formed — it is that
the schema and Fusion's own validation agree. A schema that admitted a request
the domain then rejected would recreate the failures it exists to remove, so
that agreement is asserted directly, against the real semantic model.
"""

import json

import pytest

from bench.arms import ScriptedModel, SemanticArm
from bench.constrain import (
    EXCLUDED_TRANSFORMS,
    dimension_values,
    legal_aggregations,
    orderable_columns,
    request_schema,
    table_branch,
    to_dsl,
)
from bench.dataset import Question
from bench.fixture import (
    ISLEMLER,
    ISLEMLER_COLUMNS,
    MUSTERILER,
    MUSTERILER_COLUMNS,
    as_records,
)
from bench.fusion_source import fixture_factory

TABLES = ["banka.islemler", "banka.musteriler"]


@pytest.fixture
def fusion():
    from fusion import Settings, build_app

    app = build_app(
        Settings(),
        source_factory=fixture_factory(
            {
                "musteriler": as_records(MUSTERILER, MUSTERILER_COLUMNS),
                "islemler": as_records(ISLEMLER, ISLEMLER_COLUMNS),
            }
        ),
    )
    app.sources.connect("banka", {"type": "fixture"})
    yield app
    app.close()


# -- the rules come from the domain ---------------------------------------


def test_aggregations_follow_the_measure_type(fusion):
    model = fusion.semantic.model_for("banka.islemler")
    numeric = legal_aggregations(model.measure("tutar"))
    text = legal_aggregations(model.measure("kanal"))
    rows = legal_aggregations(model.measure("*"))

    assert "sum" in numeric and "median" in numeric
    # A varchar cannot be summed, and the schema must not offer it.
    assert "sum" not in text and "avg" not in text
    assert "count" in text and "count_distinct" in text
    # The row measure counts and does nothing else.
    assert set(rows) == {"count", "count_distinct"}


def test_every_admitted_pair_survives_fusions_own_validation(fusion):
    """The property the whole approach rests on.

    Each ``measure``/``aggregation`` pair the schema allows is built into a
    real metric string and pushed through ``build``, which is what a live
    request goes through. Nothing the model can emit may fail here.
    """
    for table in TABLES:
        branch = table_branch(fusion.semantic.model_for(table), transforms=[])
        for option in branch["properties"]["metrics"]["items"]["oneOf"]:
            measure = option["properties"]["measure"]["const"]
            for agg in option["properties"]["aggregation"]["enum"]:
                fusion.semantic.build(table, [f"{measure}:{agg}"])


def test_every_admitted_dimension_survives_validation(fusion):
    for table in TABLES:
        branch = table_branch(fusion.semantic.model_for(table), transforms=[])
        for option in branch["properties"]["dimensions"]["items"]["oneOf"]:
            column = option["properties"]["column"]["const"]
            grains = option["properties"].get("grain", {}).get("enum", [""])
            for grain in grains:
                name = f"{column}:{grain}" if grain else column
                fusion.semantic.build(table, ["*:count"], [name])


def test_a_grain_is_only_offered_on_a_time_dimension(fusion):
    branch = table_branch(fusion.semantic.model_for("banka.islemler"), transforms=[])
    by_column = {
        option["properties"]["column"]["const"]: option
        for option in branch["properties"]["dimensions"]["items"]["oneOf"]
    }
    assert "grain" in by_column["islem_tarihi"]["properties"]
    # `kanal:month` is not merely invalid, it is inexpressible.
    assert "grain" not in by_column["kanal"]["properties"]
    assert by_column["kanal"]["additionalProperties"] is False


def test_ntile_is_not_offered(fusion):
    schema = request_schema(fusion.semantic, TABLES)
    offered = set()
    for branch in schema["oneOf"]:
        for option in branch["properties"]["metrics"]["items"]["oneOf"]:
            offered |= set(option["properties"]["transform"]["enum"])
    assert "cumsum" in offered and "time_shift" in offered
    # The only transform with a required argument stays out: offering it
    # without one would reintroduce the error class being removed.
    assert offered & EXCLUDED_TRANSFORMS == set()


def test_one_table_needs_no_alternation(fusion):
    schema = request_schema(fusion.semantic, ["banka.islemler"])
    assert "oneOf" not in schema
    assert schema["properties"]["table"]["const"] == "banka.islemler"


def test_each_table_carries_only_its_own_columns(fusion):
    schema = request_schema(fusion.semantic, TABLES)
    by_table = {b["properties"]["table"]["const"]: b for b in schema["oneOf"]}
    islemler = {
        o["properties"]["measure"]["const"]
        for o in by_table["banka.islemler"]["properties"]["metrics"]["items"]["oneOf"]
    }
    musteriler = {
        o["properties"]["measure"]["const"]
        for o in by_table["banka.musteriler"]["properties"]["metrics"]["items"]["oneOf"]
    }
    assert "tutar" in islemler and "tutar" not in musteriler
    assert "segment" in musteriler and "segment" not in islemler


# -- turning choices back into the DSL -------------------------------------


def test_structured_metrics_become_dsl_strings():
    request = to_dsl(
        {
            "table": "banka.islemler",
            "metrics": [
                {"measure": "tutar", "aggregation": "sum"},
                {"measure": "tutar", "aggregation": "avg", "transform": "cumsum"},
                {"measure": "*", "aggregation": "count", "transform": ""},
            ],
            "dimensions": [
                {"column": "islem_tarihi", "grain": "month"},
                {"column": "kanal"},
                {"column": "durum", "grain": ""},
            ],
        }
    )
    assert request["metrics"] == ["tutar:sum", "cumsum(tutar:avg)", "*:count"]
    assert request["dimensions"] == ["islem_tarihi:month", "kanal", "durum"]


def test_plain_strings_pass_through_unchanged():
    """The unconstrained arm's replies must survive the same path."""
    request = to_dsl({"table": "t", "metrics": ["tutar:sum"], "dimensions": ["kanal", "d:month"]})
    assert request["metrics"] == ["tutar:sum"]
    assert request["dimensions"] == ["kanal", "d:month"]


def test_descending_becomes_the_dsls_minus_prefix():
    assert to_dsl({"order_by": "tutar_sum", "descending": True})["order_by"] == "-tutar_sum"
    assert to_dsl({"order_by": "tutar_sum", "descending": False})["order_by"] == "tutar_sum"
    # Already prefixed, and asked for again: not "--tutar_sum".
    assert to_dsl({"order_by": "-tutar_sum", "descending": True})["order_by"] == "-tutar_sum"
    assert to_dsl({})["order_by"] == ""


def test_an_order_by_of_the_wrong_type_no_longer_reaches_the_dsl():
    """The 4 failures that were `order_by` as a list or an object.

    The schema types it as a string, so this cannot arrive — but `to_dsl` is
    also the place an unconstrained reply passes through, and it must not turn
    a bad value into a worse one.
    """
    assert to_dsl({"order_by": [{"column": "x"}]})["order_by"] == [{"column": "x"}]


def test_empty_choices_are_dropped_rather_than_sent_as_blanks():
    request = to_dsl({"metrics": [{"measure": "", "aggregation": ""}], "dimensions": [""]})
    assert request["metrics"] == [] and request["dimensions"] == []


# -- the arm ---------------------------------------------------------------


def test_the_constrained_arm_hands_the_schema_to_the_model(fusion):
    reply = {"table": "banka.islemler", "metrics": [{"measure": "tutar", "aggregation": "sum"}]}
    model = ScriptedModel([json.dumps(reply)])
    arm = SemanticArm("semantic-schema", model, fusion, TABLES, constrained=True)
    answer = arm.answer(Question(id="q1", text="toplam tutar?"))

    assert model.schemas[0] is not None, "decoding was not constrained"
    assert not answer.error
    assert answer.rows and answer.named == ("islemler", "tutar")


def test_the_unconstrained_arm_is_left_exactly_as_it_was(fusion):
    model = ScriptedModel([json.dumps({"table": "banka.islemler", "metrics": ["tutar:sum"]})])
    arm = SemanticArm("semantic", model, fusion, TABLES)
    answer = arm.answer(Question(id="q1", text="toplam tutar?"))

    assert model.schemas == [None]
    assert not answer.error and answer.rows


def test_the_constrained_prompt_teaches_no_syntax(fusion):
    """Nothing to teach: the grammar cannot be violated, so the prompt is meaning."""
    arm = SemanticArm("c", ScriptedModel([]), fusion, TABLES, constrained=True)
    prompt = arm.prompt_for(Question(id="q", text="toplam?"))
    assert "Example answer" not in prompt
    assert "measure:aggregation" not in prompt
    # It still names what is available, which is the arm's actual variable.
    assert "tutar" in prompt


# -- which dimensions get their values spelled out -------------------------


def _value_enums(fusion, table: str) -> dict:
    branch = next(
        b
        for b in request_schema(fusion.semantic, TABLES)["oneOf"]
        if b["properties"]["table"]["const"] == table
    )
    return {
        option["properties"]["column"]["const"]: option["properties"]["value"].get("enum")
        for option in branch["properties"]["filters"]["items"]["oneOf"]
    }


def test_a_category_gets_its_values_and_an_identifier_does_not(fusion):
    """The rule: a category repeats, an identifier does not.

    This is what stops the benchmark's own fixture from being enumerated
    wholesale — with ten rows, every column is technically low-cardinality.
    """
    islemler = _value_enums(fusion, "banka.islemler")
    assert islemler["durum"] == ["basarili", "iptal"]
    assert islemler["kanal"] == ["mobil", "sube", "web"]
    # Continuous, and a key: neither is a vocabulary.
    assert islemler["tutar"] is None
    assert islemler["islem_id"] is None


def test_personal_columns_are_not_enumerated_into_a_prompt(fusion):
    """Names and email addresses never repeat, so the rule excludes them.

    Worth asserting rather than leaving to chance: a schema built from live
    data is a place per-row values can quietly end up in a model's context.
    """
    musteriler = _value_enums(fusion, "banka.musteriler")
    assert musteriler["eposta"] is None
    assert musteriler["ad_soyad"] is None
    # The categorical columns beside them still work.
    assert musteriler["segment"] == ["premium", "standart", "temel"]
    assert musteriler["sube_kodu"] == ["ANK02", "IST01", "IZM03"]


def test_a_null_group_cannot_make_a_column_look_like_a_category(fusion):
    """`eposta` has two NULLs; its three real addresses each occur once.

    Counting the NULL group would have let it pass the repetition test, which
    is exactly how the rule failed the first time it was written.
    """
    model = fusion.semantic.model_for("banka.musteriler")
    eposta = next(d for d in model.dimensions if d.as_dict()["name"] == "eposta")
    assert dimension_values(fusion.semantic, "banka.musteriler", eposta) == []


def test_an_enumerated_column_only_takes_equality(fusion):
    """`like` against a closed vocabulary is a way to match nothing."""
    islemler_ops = {
        option["properties"]["column"]["const"]: option["properties"]["op"]["enum"]
        for option in next(
            b
            for b in request_schema(fusion.semantic, TABLES)["oneOf"]
            if b["properties"]["table"]["const"] == "banka.islemler"
        )["properties"]["filters"]["items"]["oneOf"]
    }
    assert islemler_ops["durum"] == ["eq", "ne"]
    assert "like" in islemler_ops["tutar"]


def test_a_time_dimension_is_never_enumerated(fusion):
    model = fusion.semantic.model_for("banka.islemler")
    tarih = next(d for d in model.dimensions if d.as_dict()["name"] == "islem_tarihi")
    assert dimension_values(fusion.semantic, "banka.islemler", tarih) == []


def test_the_constrained_context_names_the_values_the_schema_allows(fusion):
    """Prompt and constraint are read from one place, so they cannot drift."""
    arm = SemanticArm("c", ScriptedModel([]), fusion, TABLES, constrained=True)
    prompt = arm.prompt_for(Question(id="q", text="kac iptal var?"))
    assert "durum (basarili | iptal)" in prompt
    assert "eposta (" not in prompt


def test_the_constrained_prompt_stays_short(fusion):
    """Measured, not assumed: spelling out the pragmatics made this arm worse.

    13/40 correct with two sentences, 10/40 with a list of what to look for.
    The assertion exists so that the list does not quietly come back.
    """
    arm = SemanticArm("c", ScriptedModel([]), fusion, TABLES, constrained=True)
    prompt = arm.prompt_for(Question(id="q", text="aylik kumulatif tutar"))
    instructions = prompt.split("Table ")[0]
    assert len(instructions.splitlines()) <= 3
    assert "cumsum" not in instructions and "bazinda" not in instructions


def test_order_by_can_only_name_a_column(fusion):
    """The last hole: free text let the model write `tutar DESC` into it.

    Direction lives in `descending`, so a name never needs to carry it.
    """
    branch = next(
        b
        for b in request_schema(fusion.semantic, TABLES)["oneOf"]
        if b["properties"]["table"]["const"] == "banka.islemler"
    )
    allowed = branch["properties"]["order_by"]["enum"]
    assert "tutar_sum" in allowed and "kanal" in allowed and "" in allowed
    assert "tutar DESC" not in allowed
    assert not any(" " in name or "(" in name for name in allowed)


def test_order_by_names_come_from_the_domain(fusion):
    """Spelled by Fusion, not by a convention copied into the benchmark."""
    model = fusion.semantic.model_for("banka.islemler")
    allowed = set(orderable_columns(model, [("tutar", "sum"), ("*", "count")]))
    query = fusion.semantic.build("banka.islemler", ["tutar:sum", "*:count"], ["kanal"])
    assert set(query.output_columns()) <= allowed


# -- the arm with no model in it -------------------------------------------


def test_the_lexicon_arm_answers_without_a_model(fusion):
    """No completion call, and an answer anyway."""
    from bench.arms import LexiconArm

    arm = LexiconArm("lexicon", fusion, TABLES)
    answer = arm.answer(Question(id="q", text="Toplam işlem tutarı nedir?"))
    assert not answer.error
    assert answer.rows and answer.rows[0][0] == pytest.approx(15086.5)


def test_the_lexicon_reads_a_condition_out_of_the_question(fusion):
    """`başarılı` folds onto the stored `basarili`, so no model is needed."""
    from bench.lexicon import LexiconPlanner

    planner = LexiconPlanner(fusion.semantic, TABLES)
    plan = planner.plan("Başarılı işlemlerin toplam tutarı nedir?")
    assert plan["filters"] == [{"column": "durum", "op": "eq", "value": "basarili"}]
    assert plan["metrics"] == [{"measure": "tutar", "aggregation": "sum"}]
    # The English form has no word in common with the data at all.
    english = planner.plan("What is the total amount of successful transactions?")
    assert english["filters"] == [{"column": "durum", "op": "eq", "value": "basarili"}]


def test_a_breakdown_marker_makes_a_word_a_dimension_not_a_value(fusion):
    """`şube bazında` groups by branch; it does not filter channel to `sube`."""
    from bench.lexicon import LexiconPlanner

    plan = LexiconPlanner(fusion.semantic, TABLES).plan("Şube bazında müşteri sayısı nedir?")
    assert plan["table"] == "banka.musteriler"
    assert plan["filters"] == []
    assert plan["dimensions"] == [{"column": "sube_kodu"}]


def test_the_table_is_chosen_by_name_when_no_column_is_named(fusion):
    """ "Kaç müşteri var" names no column, only a table."""
    from bench.lexicon import LexiconPlanner

    plan = LexiconPlanner(fusion.semantic, TABLES).plan("Kaç müşteri var?")
    assert plan["table"] == "banka.musteriler"
    assert plan["metrics"] == [{"measure": "*", "aggregation": "count"}]


def test_the_lexicon_cannot_name_anything_that_does_not_exist(fusion):
    """Same guarantee as the constrained arm, by a different route."""
    from bench.lexicon import LexiconPlanner

    planner = LexiconPlanner(fusion.semantic, TABLES)
    for text in ("kripto bakiyesi ne kadar", "show me the flux capacitor by quarter", ""):
        plan = planner.plan(text)
        model = fusion.semantic.model_for(plan["table"])
        for metric in plan["metrics"]:
            assert metric["measure"] in {m.name for m in model.measures}
        for dimension in plan["dimensions"]:
            assert dimension["column"] in {d.as_dict()["name"] for d in model.dimensions}


# -- a lexicon the model writes, not a person ------------------------------


def test_generation_is_confined_to_names_that_exist(fusion):
    """The model chooses what a column is called, never what columns there are."""
    from bench.lexicon_gen import lexicon_schema

    schema = lexicon_schema(fusion.semantic, TABLES)
    columns = schema["properties"]["columns"]
    assert "tutar" in columns["properties"] and "islemler" in columns["properties"]
    assert columns["additionalProperties"] is False
    # Stored values are addressed by their folded form, which is the key the
    # planner looks them up by.
    values = schema["properties"]["values"]["properties"]
    assert "basarili" in values and "iptal" in values
    assert "eposta" not in columns["properties"].get("values", {})


def test_a_generated_lexicon_replaces_the_hand_written_one(fusion):
    """Injected words reach matching, so a generated file actually takes effect."""
    from bench.lexicon import LexiconPlanner

    planner = LexiconPlanner(
        fusion.semantic,
        TABLES,
        synonyms={"tutar": ["hacim", "ciro"]},
        value_synonyms={"basarili": ["successful"]},
    )
    plan = planner.plan("Toplam ciro nedir?")
    assert plan["metrics"] == [{"measure": "tutar", "aggregation": "sum"}]
    english = planner.plan("total amount of successful transactions")
    assert english["filters"] == [{"column": "durum", "op": "eq", "value": "basarili"}]


def test_a_generated_lexicon_round_trips_through_a_file(tmp_path, fusion):
    from bench.lexicon_gen import load, save

    written = {"columns": {"tutar": ["ciro"]}, "values": {"basarili": ["successful"]}}
    path = save(written, tmp_path / "banka.json")
    assert load(path) == written


def test_generation_asks_about_every_name_it_will_accept(fusion):
    """Prompt and schema are built from one vocabulary, so neither can lag."""
    from bench.lexicon_gen import generation_prompt, lexicon_schema

    prompt = generation_prompt(fusion.semantic, TABLES)
    for name in lexicon_schema(fusion.semantic, TABLES)["properties"]["columns"]["properties"]:
        assert name in prompt


# -- the deterministic proposal, corrected by a model ----------------------


def _repair_arm(fusion, replies):
    from bench.arms import RepairArm

    model = ScriptedModel(replies)
    return RepairArm("repair", model, fusion, TABLES), model


def test_the_repair_arm_starts_from_the_lexicons_proposal(fusion):
    """The model is shown a first attempt, not an empty page."""
    reply = {"table": "banka.islemler", "metrics": [{"measure": "tutar", "aggregation": "sum"}]}
    arm, model = _repair_arm(fusion, [json.dumps(reply)])
    arm.answer(Question(id="q1", text="Başarılı işlemlerin toplam tutarı nedir?"))

    assert arm.seeds["q1"]["metrics"] == [{"measure": "tutar", "aggregation": "sum"}]
    assert "First attempt" in model.prompts[0]
    assert "tutar" in model.prompts[0]
    assert model.schemas[0] is not None, "the correction was not constrained"


def test_a_model_failure_is_reported_not_swallowed(fusion):
    """Measurement integrity: the arm must not become the lexicon arm.

    Quietly running the proposal when the model is unreachable would blend two
    arms into one number, and nothing in the run would show it happened.
    """
    arm, _ = _repair_arm(fusion, ["not json at all"])
    answer = arm.answer(Question(id="q1", text="Toplam işlem tutarı nedir?"))

    assert "model failed" in answer.error
    assert not answer.fell_back


def test_a_seeded_arm_can_keep_its_proposal_and_say_so(fusion):
    """The product setting: an answer, and a record that it was the fallback."""
    from bench.arms import RepairArm

    arm = RepairArm("repair", ScriptedModel(["not json"]), fusion, TABLES, fall_back=True)
    answer = arm.answer(Question(id="q1", text="Toplam işlem tutarı nedir?"))

    assert not answer.error
    assert answer.fell_back
    assert answer.rows and answer.rows[0][0] == pytest.approx(15086.5)


def test_a_correction_is_taken_when_the_model_gives_one(fusion):
    corrected = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "sum"}],
        "dimensions": [{"column": "kanal"}],
        "filters": [],
    }
    arm, _ = _repair_arm(fusion, [json.dumps(corrected)])
    answer = arm.answer(Question(id="q1", text="Kanal bazında toplam tutar?"))

    assert not answer.error
    # One row per channel, rather than the single total the seed would give.
    assert len(answer.rows) == 3


def test_a_correction_still_cannot_name_something_that_does_not_exist(fusion):
    """Constrained decoding applies to the repair, not just the first answer."""
    correction = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "avg"}],
        "dimensions": [{"column": "durum"}],
    }
    arm, _ = _repair_arm(fusion, [json.dumps(correction)])
    answer = arm.answer(Question(id="q1", text="Toplam tutar?"))
    assert not answer.error
    model = fusion.semantic.model_for("banka.islemler")
    for name in answer.named or ():
        assert name in {m.name for m in model.measures} | {
            d.as_dict()["name"] for d in model.dimensions
        } | {"islemler", "musteriler"}


def test_a_correction_that_will_not_run_can_be_refused(fusion):
    """For a product: never replace a proposal that works with one that does not.

    Off by default, because the measured arm has to report what the model did
    rather than what a safety net rescued.
    """
    from bench.arms import RepairArm

    broken = json.dumps({"table": "banka.islemler", "metrics": [], "dimensions": []})
    question = Question(id="q1", text="Toplam işlem tutarı nedir?")

    unguarded = RepairArm("r", ScriptedModel([broken]), fusion, TABLES)
    assert unguarded.answer(question).error

    guarded = RepairArm("r", ScriptedModel([broken]), fusion, TABLES, fall_back=True)
    rescued = guarded.answer(question)
    assert not rescued.error
    assert rescued.rows[0][0] == pytest.approx(15086.5)


def test_a_lexicon_is_normalised_on_the_way_in(fusion):
    """`_clean` was defined and never called, so none of this happened.

    A lexicon is a file a model wrote and a person may edit. A leading space
    makes an entry dead on arrival — the match anchors on a word boundary —
    and an entry that only repeats the name contributes nothing while looking
    as if it did.
    """
    from bench.lexicon import LexiconPlanner

    planner = LexiconPlanner(
        fusion.semantic,
        TABLES,
        synonyms={"tutar": [" ciro", "TUTARI", "hacim"]},
        value_synonyms={"basarili": [" successful "]},
    )

    # The space is gone, so the word actually matches.
    assert planner.plan("Toplam ciro nedir?")["metrics"] == [
        {"measure": "tutar", "aggregation": "sum"}
    ]
    assert planner.plan("total amount of successful transactions")["filters"] == [
        {"column": "durum", "op": "eq", "value": "basarili"}
    ]
    # An echo of the name is dropped: the stem match already covers inflection.
    assert "tutari" not in planner._synonyms["tutar"]
    assert planner._synonyms["tutar"] == ("ciro", "hacim")


def test_the_vocabulary_can_be_handed_over_instead_of_rediscovered(fusion):
    """Each discovery is a full DSL round trip; the schema already has them."""
    from bench.constrain import values_by_table
    from bench.lexicon import LexiconPlanner

    schema = request_schema(fusion.semantic, TABLES)
    planner = LexiconPlanner(fusion.semantic, TABLES, values=values_by_table(schema))

    assert planner._values["banka.islemler"]["durum"] == ["basarili", "iptal"]
    # And it still plans from them.
    assert planner.plan("iptal edilen islemlerin sayisi")["filters"] == [
        {"column": "durum", "op": "eq", "value": "iptal"}
    ]


# -- what the report has to show -------------------------------------------


def test_a_fallback_is_counted_and_shown(fusion):
    """An arm that substitutes its proposal is two arms reported as one.

    The flag reached `Answer` and stopped there: `run_arm` turned answers into
    grades and dropped it, so a run where the model failed twenty times read
    exactly like one where it answered all forty.
    """
    from bench.arms import RepairArm
    from bench.dataset import Question, QuestionSet
    from bench.runner import build_report, run_arm

    questions = (
        Question(id="q1", text="Toplam işlem tutarı nedir?", gold={"duckdb": "SELECT 1"}),
        Question(id="q2", text="Kaç işlem var?", gold={"duckdb": "SELECT 1"}),
    )
    arm = RepairArm(
        "repair", ScriptedModel(["not json", "not json"]), fusion, TABLES, fall_back=True
    )
    result = run_arm(arm, questions, {}, "duckdb", [])

    assert result.fallbacks(["q1", "q2"]) == 2
    assert all(g.fell_back for g in result.grades.values())

    report = build_report(QuestionSet(name="t", questions=questions), "duckdb", [result])
    assert "Fell back" in report.to_markdown()
    assert report.as_dict()["arms"][0]["fallbacks"] == 2


def test_a_clean_run_shows_no_fallback_column(fusion):
    """Reports from runs where nothing fell back stay exactly as they were."""
    from bench.arms import LexiconArm
    from bench.dataset import Question, QuestionSet
    from bench.runner import build_report, run_arm

    questions = (Question(id="q1", text="Toplam işlem tutarı nedir?", gold={"duckdb": "SELECT 1"}),)
    result = run_arm(LexiconArm("lexicon", fusion, TABLES), questions, {}, "duckdb", [])

    assert result.fallbacks(["q1"]) == 0
    report = build_report(QuestionSet(name="t", questions=questions), "duckdb", [result])
    assert "Fell back" not in report.to_markdown()


# -- the vocabulary is discovered once, and covers every table -------------


def test_the_schema_is_read_once_not_rediscovered(fusion, monkeypatch):
    """Each discovery is a full DSL round trip; three passes was 42 of them.

    Measured by hand when the fix landed and left unguarded, which is exactly
    how a constructor reorder would put them back.
    """
    import bench.constrain as constrain
    from bench.arms import RepairArm

    calls = {"n": 0}
    original = constrain.dimension_values

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(constrain, "dimension_values", counting)
    dimensions = sum(len(fusion.semantic.model_for(t).dimensions) for t in TABLES)

    RepairArm("repair", ScriptedModel([]), fusion, TABLES)

    assert calls["n"] == dimensions, f"{calls['n']} discoveries for {dimensions} dimensions"


def test_values_by_table_covers_every_table_it_was_given(fusion):
    """A table missing from the map gets an empty vocabulary and no error."""
    from bench.constrain import values_by_table

    many = values_by_table(request_schema(fusion.semantic, TABLES))
    assert set(many) == set(TABLES)

    # The single-table schema has no `oneOf` and takes the other branch.
    one = values_by_table(request_schema(fusion.semantic, ["banka.islemler"]))
    assert set(one) == {"banka.islemler"}
    assert one["banka.islemler"]["durum"] == ["basarili", "iptal"]


def test_describe_tables_refuses_an_empty_list(fusion):
    """`request_schema` gained this guard; the function beside it did not."""
    from bench.arms import describe_tables

    with pytest.raises(ValueError, match="at least one table"):
        describe_tables(fusion, [])
