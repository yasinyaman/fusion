"""Healthcare: one hospital information system, behind a gateway that refuses raw SQL."""

from __future__ import annotations

from scenarios.harness import Ctx, scenario

VISITS = "hospital.visits"
LABS = "hospital.lab_results"
RX = "hospital.prescriptions"


@scenario(
    "H01",
    sector="healthcare",
    title="30-day readmissions by department",
    sources=("hospital",),
    capabilities=("refusal", "load_table", "window", "nulls", "semantic"),
)
def readmissions(ctx: Ctx) -> None:
    """Quality asks how many inpatients come back within 30 days of discharge. That is
    a window over each patient's visits, which cannot be planned over a table too big
    to load; the inpatient stays are loaded as a slice and the window runs there."""

    def readmit(table: str, where: str = "") -> str:
        return f"""
            WITH stays AS (
                SELECT department, discharged_at,
                       lead(admitted_at) OVER (PARTITION BY patient_id
                                               ORDER BY admitted_at, visit_id) AS next_admission
                FROM {table} {where}
            )
            SELECT department, COUNT(*) AS discharges,
                   COUNT(*) FILTER (WHERE next_admission <= discharged_at + INTERVAL 30 DAY)
                       AS readmitted,
                   COUNT(*) FILTER (WHERE next_admission <= discharged_at + INTERVAL 30 DAY)
                       / COUNT(*) AS rate
            FROM stays WHERE discharged_at IS NOT NULL
            GROUP BY department ORDER BY department"""

    ctx.refused(readmit(VISITS), "the window over every visit is refused")
    where = "visit_type = 'inpatient'"
    loaded = ctx.tool(
        "load_table",
        {
            "table": VISITS,
            "where": where,
            "columns": ["visit_id", "patient_id", "department", "admitted_at", "discharged_at"],
        },
        "load the inpatient stays only",
    )
    ctx.sql(
        readmit(loaded.get("slice_table", VISITS)),
        "readmission rate per department",
        ordered=True,
        oracle_sql=readmit(VISITS, f"WHERE {where}"),
        max_moved=0,
        tolerance=1e-12,
    )
    ctx.check(
        "the slice is a small part of the visits table",
        loaded.get("row_count", 0) < ctx.rows(VISITS) // 10,
        f"{loaded.get('row_count'):,} of {ctx.rows(VISITS):,} rows",
    )
    ctx.tool_rows(
        "query_metrics",
        {
            "table": VISITS,
            "metrics": ["total_cost:median", "total_cost:sum", "*:count"],
            "dimensions": ["department"],
            "filters": [
                {"column": "visit_type", "value": "inpatient"},
                {"column": "admitted_at", "op": "gte", "value": "2026-01-01"},
            ],
        },
        f"""SELECT department, MEDIAN(total_cost), SUM(total_cost), COUNT(*) FROM {VISITS}
            WHERE visit_type = 'inpatient' AND admitted_at >= '2026-01-01'
            GROUP BY 1 ORDER BY 1""",
        "inpatient cost per department this year",
        ordered=True,
        tolerance=0.0,
    )
    declared = {c.name: c.type for c in ctx.app.catalog.get_table(VISITS).columns}
    for held in ctx.app.catalog.slices_of(VISITS):
        if held.spec.columns and "total_cost" in held.spec.columns:
            stored = {c.name: c.type for c in ctx.app.store.describe(held.table_name)}
            ctx.check(
                "the slice keeps the source's decimal precision and scale",
                stored.get("total_cost", "").replace(" ", "").upper()
                == declared["total_cost"].replace(" ", "").upper(),
                f"source {declared['total_cost']}, store {stored.get('total_cost')}",
            )
            break
    ctx.sql(
        f"SELECT department, COUNT(*) AS still_admitted FROM {VISITS} "
        "WHERE discharged_at IS NULL GROUP BY department ORDER BY department",
        "patients with no discharge time: IS NULL travels to the source",
        ordered=True,
        actions={VISITS: ("load_slice", "reuse")},
        max_moved=ctx.rows(VISITS) // 50,
    )


@scenario(
    "H02",
    sector="healthcare",
    title="Diabetic patients' HbA1c results, pending ones included",
    sources=("hospital",),
    capabilities=("two-slices", "nulls", "join"),
)
def diabetic_labs(ctx: Ctx) -> None:
    """Endocrinology wants every HbA1c result of visits coded E11.9. Both tables are
    millions of rows; each is narrowed by its own condition, and results that are still
    pending (no value yet) must be counted, not silently dropped."""
    query = (
        "SELECT p.gender, COUNT(*) AS results, COUNT(l.result_value) AS resulted, "
        "COUNT(*) - COUNT(l.result_value) AS pending, AVG(l.result_value) AS mean, "
        "COUNT(*) FILTER (WHERE l.result_value > l.ref_high) AS above_range "
        f"FROM hospital.diagnoses d JOIN {LABS} l ON l.visit_id = d.visit_id "
        "JOIN hospital.patients p ON p.patient_id = l.patient_id "
        "WHERE d.icd10 = 'E11.9' AND l.test_code = 'HBA1C' GROUP BY p.gender ORDER BY p.gender"
    )
    result = ctx.sql(
        query,
        "HbA1c of diabetic visits by gender",
        ordered=True,
        tolerance=1e-9,
        actions={"hospital.diagnoses": "load_slice", LABS: "load_slice"},
    )
    if result is not None and result.rows:
        ctx.check("pending results are counted", sum(r[3] for r in result.rows) > 0)
    ctx.sql(
        f"SELECT test_code, COUNT(*) AS waiting FROM {LABS} "
        "WHERE result_value IS NULL AND resulted_at < '2026-10-29' "
        "GROUP BY test_code ORDER BY test_code",
        "results still pending after two days",
        ordered=True,
        actions={LABS: "load_slice"},
        max_moved=ctx.rows(LABS) // 20,
    )
    held = {
        s.table_name: sorted(s.spec.columns or ["*"])
        for s in ctx.app.catalog.all_slices()
        if s.ref.table == "patients"
    }
    ctx.note("patients is small enough to load whole, so its name columns came too", str(held))


@scenario(
    "H03",
    sector="healthcare",
    title="Drug interactions: two drugs prescribed to one patient within the supply",
    sources=("hospital",),
    capabilities=("self-join", "load_table", "unicode", "limit"),
)
def interactions(ctx: Ctx) -> None:
    """Pharmacy screens for warfarin given together with clopidogrel or ibuprofen. The
    question is a self-join of prescriptions; the engine will not slice one table read
    under two aliases, so the three drugs are loaded first and joined on the slice."""

    def pairs(table: str) -> str:
        return f"""
            SELECT b.drug_name AS second_drug, COUNT(DISTINCT a.patient_id) AS patients,
                   COUNT(*) AS overlaps
            FROM {table} a JOIN {table} b
              ON a.patient_id = b.patient_id AND a.drug_name = 'Varfarin'
             AND b.drug_name IN ('Klopidogrel', 'İbuprofen')
             AND b.prescribed_on BETWEEN a.prescribed_on AND a.prescribed_on + a.days_supply
            GROUP BY b.drug_name ORDER BY b.drug_name"""

    ctx.limit(pairs(RX), "the self-join on the raw table", ordered=True)
    loaded = ctx.tool(
        "load_table",
        {
            "table": RX,
            "where": "drug_name IN ('Varfarin', 'Klopidogrel', 'İbuprofen')",
            "columns": ["patient_id", "drug_name", "prescribed_on", "days_supply"],
        },
        "load the three drugs (one name starts with a dotted capital İ)",
    )
    expected = ctx.scalar(
        f"SELECT count(*) FROM {RX} WHERE drug_name IN ('Varfarin', 'Klopidogrel', 'İbuprofen')"
    )
    ctx.check(
        "the slice holds every prescription of the three drugs",
        loaded.get("row_count") == expected,
        f"{loaded.get('row_count')} vs {expected} at the source",
    )
    table = loaded.get("slice_table", RX)
    ctx.sql(
        pairs(table),
        "overlapping prescriptions on the slice (DATE + SMALLINT, valid at the source)",
        ordered=True,
        oracle_sql=pairs(RX),
        max_moved=0,
    )
    cast = "a.prescribed_on + CAST(a.days_supply AS INTEGER)"
    ctx.sql(
        pairs(table).replace("a.prescribed_on + a.days_supply", cast),
        "the same with the supply cast to INTEGER",
        ordered=True,
        oracle_sql=pairs(RX).replace("a.prescribed_on + a.days_supply", cast),
        max_moved=0,
    )
    ctx.sql(
        f"SELECT drug_name, COUNT(*) AS n FROM {RX} WHERE drug_name LIKE 'İ%' "
        "GROUP BY drug_name ORDER BY drug_name",
        "LIKE with a Turkish capital",
        ordered=True,
        actions={RX: "load_slice"},
    )
    ctx.sql(
        f"SELECT drug_name, COUNT(*) AS n FROM {RX} WHERE drug_name = 'ibuprofen' "
        "GROUP BY drug_name",
        "a lower-case name matches nothing, as in the source",
    )
