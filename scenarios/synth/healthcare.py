"""Healthcare: one hospital information system.

A visit's patient and admission time are functions of the visit id, so the
prescriptions and lab results of a visit carry the right patient without a
join at generation time.
"""

from __future__ import annotations

from scenarios.synth.core import (
    FIRST_NAMES,
    LAST_NAMES,
    Database,
    Table,
    city,
    sql_list,
    tpl,
    weighted,
)

DEPARTMENTS = [
    "Kardiyoloji", "Dahiliye", "Ortopedi", "Nöroloji", "Göz", "KBB", "Üroloji", "Onkoloji",
    "Pediatri", "Kadın Doğum", "Acil", "Dermatoloji", "Psikiyatri", "Endokrinoloji",
    "Göğüs Hastalıkları",
]  # fmt: skip
SITES = ["Merkez", "Çamlıca", "Bağcılar", "Çankaya", "Karşıyaka"]
ICD10 = [
    "I10", "E11.9", "J06.9", "M54.5", "K21.9", "E78.5", "J45.9", "N39.0", "F41.1", "I25.1",
    "E03.9", "J18.9", "K29.7", "M17.9", "G43.9", "I48", "N18.3", "C50.9", "F32.9", "L20.9",
    "H52.1", "J44.9", "E66.9", "D50.9", "R51", "A09", "B34.9", "S93.4", "O80", "Z00.0",
]  # fmt: skip
DRUGS = [
    "Metformin", "Atorvastatin", "Ramipril", "Amlodipin", "Parasetamol", "İbuprofen",
    "Omeprazol", "Levotiroksin", "Salbutamol", "Amoksisilin", "Varfarin", "İnsülin Glarjin",
    "Sertralin", "Metoprolol", "Furosemid", "Klopidogrel", "Pantoprazol", "Diklofenak",
]  # fmt: skip
LAB_TESTS = [
    "HBA1C", "LDL", "HDL", "CRP", "TSH", "ALT", "AST", "KREATININ", "HGB", "GLUKOZ", "D_VIT",
    "B12",
]  # fmt: skip

#: The patient of a visit and when it started: shared by every table keyed on a visit.
VISIT_PATIENT = "skew({v}, 'v_pat', {{patients}}, 1.6)"
VISIT_START = "ts_at({v}, 'v_ts')"

DOCTORS = Table(
    name="doctors",
    base=3_000,
    floor=45,
    primary_key=("doctor_id",),
    select=tpl(
        """
        SELECT CAST(i AS INTEGER) AS doctor_id,
               pick(i, 'd_t', ['Uzm. Dr.', 'Doç. Dr.', 'Prof. Dr.', 'Op. Dr.']) || ' '
                   || pick(i, 'd_fn', <first>) || ' ' || pick(i, 'd_ln', <last>) AS full_name,
               <departments>[1 + (i - 1) % <department_count>] AS department,
               pick(i, 'd_site', <sites>) AS site
        FROM range(1, {n} + 1) t(i)
        """,
        first=sql_list(FIRST_NAMES),
        last=sql_list(LAST_NAMES),
        departments=sql_list(DEPARTMENTS),
        department_count=str(len(DEPARTMENTS)),
        sites=sql_list(SITES),
    ),
)

PATIENTS = Table(
    name="patients",
    base=300_000,
    floor=800,
    primary_key=("patient_id",),
    indexes=(("birth_date",), ("city",)),
    select=tpl(
        """
        SELECT CAST(i AS BIGINT) AS patient_id,
               uuid_of(i, 'mrn') AS mrn,
               pick(i, 'p_fn', <first>) AS first_name,
               pick(i, 'p_ln', <last>) AS last_name,
               <gender> AS gender,
               DATE '1932-01-01' + CAST(floor(u(i, 'p_bd') * 34000) AS INTEGER) AS birth_date,
               <city> AS city,
               pick(i, 'p_bt', ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', '0+', '0-']) AS blood_type,
               <insurance> AS insurance_type,
               u(i, 'p_chr') < 0.22 AS chronic_flag
        FROM range(1, {n} + 1) t(i)
        """,
        first=sql_list(FIRST_NAMES),
        last=sql_list(LAST_NAMES),
        city=city("i", "p_city"),
        gender=weighted("u(i, 'p_g')", [("K", 0.52), ("E", 0.48)]),
        insurance=weighted("u(i, 'p_ins')", [("SGK", 0.78), ("özel", 0.15), ("yok", 0.07)]),
    ),
)

VISITS = Table(
    name="visits",
    base=2_000_000,
    floor=8_000,
    primary_key=("visit_id",),
    indexes=(("patient_id",), ("admitted_at",), ("department",)),
    select=tpl(
        """
        SELECT visit_id,
               patient_id,
               doctor_id,
               admitted_at,
               CASE WHEN u(visit_id, 'v_on') < 0.005 THEN NULL
                    WHEN visit_type = 'inpatient'
                    THEN admitted_at + to_hours(
                        CAST(24 + floor(lognorm(visit_id, 'v_los', 4.2, 0.8)) AS BIGINT))
                    ELSE admitted_at + to_minutes(
                        CAST(20 + floor(u(visit_id, 'v_len') * 340) AS BIGINT))
               END AS discharged_at,
               visit_type,
               <departments>[1 + (doctor_id - 1) % <department_count>] AS department,
               CAST(round(lognorm(visit_id, 'v_cost',
                                  CASE visit_type WHEN 'inpatient' THEN 9.6
                                                  WHEN 'emergency' THEN 7.4 ELSE 6.2 END,
                                  0.7), 2) AS DECIMAL(12, 2)) AS total_cost,
               pick(visit_id, 'v_site', <sites>) AS site
        FROM (
            SELECT CAST(i AS BIGINT) AS visit_id,
                   <patient> AS patient_id,
                   CAST(1 + hash(i, 'v_doc') % {doctors} AS INTEGER) AS doctor_id,
                   <start> AS admitted_at,
                   <kind> AS visit_type
            FROM range(1, {n} + 1) t(i)
        )
        """,
        departments=sql_list(DEPARTMENTS),
        department_count=str(len(DEPARTMENTS)),
        sites=sql_list(SITES),
        patient=VISIT_PATIENT.format(v="i"),
        start=VISIT_START.format(v="i"),
        kind=weighted(
            "u(i, 'v_ty')", [("outpatient", 0.80), ("emergency", 0.14), ("inpatient", 0.06)]
        ),
    ),
)

DIAGNOSES = Table(
    name="diagnoses",
    base=3_000_000,
    floor=12_000,
    primary_key=("diagnosis_id",),
    indexes=(("visit_id",), ("icd10",)),
    select=tpl(
        """
        SELECT CAST(i AS BIGINT) AS diagnosis_id,
               CAST(1 + hash(i, 'dg_v') % {visits} AS BIGINT) AS visit_id,
               <codes>[skew(i, 'dg_c', <code_count>, 1.8)] AS icd10,
               u(i, 'dg_p') < 0.60 AS is_primary
        FROM range(1, {n} + 1) t(i)
        """,
        codes=sql_list(ICD10),
        code_count=str(len(ICD10)),
    ),
)

PRESCRIPTIONS = Table(
    name="prescriptions",
    base=2_500_000,
    floor=10_000,
    primary_key=("rx_id",),
    indexes=(("patient_id",), ("visit_id",), ("drug_code",)),
    select=tpl(
        """
        SELECT rx_id,
               visit_id,
               <patient> AS patient_id,
               'ATC-' || lpad(CAST(drug AS VARCHAR), 3, '0') AS drug_code,
               <drugs>[drug] AS drug_name,
               round(0.5 + 4 * u(rx_id, 'rx_dose'), 1) AS daily_dose,
               CAST(pick(rx_id, 'rx_days', [7, 10, 14, 28, 30, 90]) AS SMALLINT) AS days_supply,
               CAST(<start> AS DATE) AS prescribed_on
        FROM (
            SELECT CAST(i AS BIGINT) AS rx_id,
                   CAST(1 + hash(i, 'rx_v') % {visits} AS BIGINT) AS visit_id,
                   skew(i, 'rx_dr', <drug_count>, 1.5) AS drug
            FROM range(1, {n} + 1) t(i)
        )
        """,
        drugs=sql_list(DRUGS),
        drug_count=str(len(DRUGS)),
        patient=VISIT_PATIENT.format(v="visit_id"),
        start=VISIT_START.format(v="visit_id"),
    ),
)

LAB_RESULTS = Table(
    name="lab_results",
    base=4_000_000,
    floor=16_000,
    primary_key=("lab_id",),
    indexes=(("patient_id",), ("visit_id",), ("test_code",), ("resulted_at",)),
    description="Lab results; 3% are still pending and have no value.",
    select=tpl(
        """
        SELECT lab_id,
               visit_id,
               <patient> AS patient_id,
               test_code,
               CASE WHEN u(lab_id, 'lb_pend') < 0.03 THEN NULL
                    ELSE round(lognorm(lab_id, 'lb_v', 1.6, 0.5), 2) END AS result_value,
               pick(test_code, 'lb_unit', ['mg/dL', 'mmol/L', 'U/L', 'g/dL', '%']) AS unit,
               2.0 + (hash(test_code, 'lb_lo') % 10) / 10.0 AS ref_low,
               8.0 + (hash(test_code, 'lb_hi') % 20) / 10.0 AS ref_high,
               <start> + to_minutes(CAST(30 + hash(lab_id, 'lb_lag') % 2880 AS BIGINT))
                   AS resulted_at
        FROM (
            SELECT CAST(i AS BIGINT) AS lab_id,
                   CAST(1 + hash(i, 'lb_vis') % {visits} AS BIGINT) AS visit_id,
                   pick(i, 'lb_t', <tests>) AS test_code
            FROM range(1, {n} + 1) t(i)
        )
        """,
        tests=sql_list(LAB_TESTS),
        patient=VISIT_PATIENT.format(v="visit_id"),
        start=VISIT_START.format(v="visit_id"),
    ),
)

DATABASES = (
    Database(
        name="hospital",
        sector="healthcare",
        engine="postgres",
        pushdown=False,
        description="Hospital information system.",
        tables=(DOCTORS, PATIENTS, VISITS, DIAGNOSES, PRESCRIPTIONS, LAB_RESULTS),
    ),
)
