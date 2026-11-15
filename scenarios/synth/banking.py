"""Banking: a core-banking system and a card platform that do not share a database.

``bank_core`` refuses raw SQL, as a core banking gateway would, so everything
read from it travels as a slice. ``bank_cards`` answers SQL, so a question that
stays inside it is pushed down whole.
"""

from __future__ import annotations

from scenarios.synth.core import (
    FIRST_NAMES,
    LAST_NAMES,
    Database,
    Table,
    city,
    region_of,
    sql_list,
    tpl,
    weighted,
)

BRANCHES = Table(
    name="branches",
    base=250,
    floor=12,
    primary_key=("branch_id",),
    description="Branch master data.",
    select=tpl(
        """
        SELECT CAST(i AS INTEGER) AS branch_id,
               'TR-' || lpad(CAST(i AS VARCHAR), 4, '0') AS branch_code,
               c AS city,
               <region> AS region,
               DATE '1990-01-01' + CAST(floor(u(i, 'b_open') * 12000) AS INTEGER) AS opened_on,
               u(i, 'b_dig') < 0.08 AS is_digital
        FROM (SELECT i, <city> AS c FROM range(1, {n} + 1) t(i))
        """,
        city=city("i", "b_city"),
        region=region_of("c"),
    ),
)

CUSTOMERS = Table(
    name="customers",
    base=200_000,
    floor=500,
    primary_key=("customer_id",),
    indexes=(("branch_id",), ("segment",)),
    description="Retail and SME customers; 15% have no e-mail on file.",
    select=tpl(
        """
        SELECT CAST(i AS BIGINT) AS customer_id,
               md5('tckn' || CAST(i AS VARCHAR)) AS national_id_hash,
               pick(i, 'c_fn', <first>) AS first_name,
               pick(i, 'c_ln', <last>) AS last_name,
               <segment> AS segment,
               CAST(skew(i, 'c_br', {branches}, 1.5) AS INTEGER) AS branch_id,
               DATE '1945-01-01' + CAST(floor(u(i, 'c_bd') * 22000) AS INTEGER) AS birth_date,
               <city> AS city,
               CAST(1 + floor(pow(u(i, 'c_risk'), 2.5) * 100) AS SMALLINT) AS risk_score,
               <kyc> AS kyc_status,
               TIMESTAMP '2015-01-01 00:00:00'
                   + to_seconds(CAST(floor(u(i, 'c_cr') * 370000000) AS BIGINT)) AS created_at,
               CASE WHEN u(i, 'c_em') < 0.15 THEN NULL
                    ELSE 'musteri' || CAST(i AS VARCHAR) || '@example.com' END AS email
        FROM range(1, {n} + 1) t(i)
        """,
        first=sql_list(FIRST_NAMES),
        last=sql_list(LAST_NAMES),
        city=city("i", "c_city"),
        segment=weighted(
            "u(i, 'c_seg')", [("mass", 0.70), ("affluent", 0.20), ("sme", 0.06), ("private", 0.04)]
        ),
        kyc=weighted("u(i, 'c_kyc')", [("verified", 0.90), ("pending", 0.07), ("expired", 0.03)]),
    ),
)

ACCOUNTS = Table(
    name="accounts",
    base=350_000,
    floor=900,
    primary_key=("account_id",),
    indexes=(("customer_id",),),
    select=tpl(
        """
        SELECT CAST(i AS BIGINT) AS account_id,
               skew(i, 'a_cu', {customers}, 1.3) AS customer_id,
               <kind> AS account_type,
               <currency> AS currency,
               CAST(round(lognorm(i, 'a_bal', 8.5, 1.8), 2) AS DECIMAL(18, 2)) AS balance,
               DATE '2010-01-01' + CAST(floor(u(i, 'a_op') * 6100) AS INTEGER) AS opened_on,
               <status> AS status,
               'TR' || lpad(CAST(i AS VARCHAR), 24, '0') AS iban
        FROM range(1, {n} + 1) t(i)
        """,
        kind=weighted(
            "u(i, 'a_ty')",
            [("checking", 0.55), ("savings", 0.28), ("fx", 0.12), ("investment", 0.05)],
        ),
        currency=weighted("u(i, 'a_ccy')", [("TRY", 0.80), ("USD", 0.12), ("EUR", 0.08)]),
        status=weighted("u(i, 'a_st')", [("active", 0.88), ("dormant", 0.08), ("closed", 0.04)]),
    ),
)

# Accounts whose id divides by 499 make cash deposits just under the 50,000
# reporting threshold: the structuring pattern the AML scenario has to find.
TRANSACTIONS = Table(
    name="transactions",
    base=5_000_000,
    floor=20_000,
    primary_key=("txn_id",),
    indexes=(("txn_date",), ("account_id",), ("txn_type",)),
    description="Account movements over two years; heavy accounts dominate.",
    select=tpl(
        """
        SELECT txn_id,
               account_id,
               txn_ts,
               CAST(txn_ts AS DATE) AS txn_date,
               CAST(CASE WHEN structuring THEN 45000 + floor(u(txn_id, 't_st2') * 4900)
                         ELSE round(lognorm(txn_id, 't_amt', 5.6, 1.5), 2) END
                    AS DECIMAL(18, 2)) AS amount,
               CASE WHEN structuring THEN 'credit' ELSE <direction> END AS direction,
               CASE WHEN structuring THEN pick(txn_id, 't_sch', ['branch', 'atm'])
                    ELSE <channel> END AS channel,
               CASE WHEN structuring THEN 'cash_deposit' ELSE <kind> END AS txn_type,
               <currency> AS currency,
               CASE WHEN u(txn_id, 't_cp') < 0.30 THEN NULL
                    ELSE 'TR' || lpad(CAST(hash(txn_id, 't_cpi') % 10000000000 AS VARCHAR), 24, '0')
               END AS counterparty_iban,
               <status> AS status,
               u(txn_id, 't_fl') < 0.003 AS is_flagged
        FROM (
            SELECT txn_id, account_id, txn_ts,
                   (account_id % 499 = 0 AND u(txn_id, 't_st') < 0.6) AS structuring
            FROM (
                SELECT CAST(i AS BIGINT) AS txn_id,
                       skew(i, 't_acc', {accounts}, 2.0) AS account_id,
                       ts_at(i, 't_ts') AS txn_ts
                FROM range(1, {n} + 1) t(i)
            )
        )
        """,
        direction=weighted("u(txn_id, 't_dir')", [("debit", 0.58), ("credit", 0.42)]),
        channel=weighted(
            "u(txn_id, 't_ch')",
            [("mobile", 0.55), ("web", 0.15), ("atm", 0.12), ("pos", 0.10), ("branch", 0.08)],
        ),
        kind=weighted(
            "u(txn_id, 't_ty')",
            [
                ("transfer", 0.34),
                ("payment", 0.30),
                ("eft", 0.14),
                ("cash_withdrawal", 0.12),
                ("cash_deposit", 0.07),
                ("fee", 0.03),
            ],
        ),
        currency=weighted("u(txn_id, 't_ccy')", [("TRY", 0.85), ("USD", 0.10), ("EUR", 0.05)]),
        status=weighted(
            "u(txn_id, 't_stt')", [("posted", 0.97), ("reversed", 0.02), ("pending", 0.01)]
        ),
    ),
)

LOANS = Table(
    name="loans",
    base=60_000,
    floor=300,
    primary_key=("loan_id",),
    indexes=(("customer_id",),),
    select=tpl(
        """
        SELECT CAST(i AS BIGINT) AS loan_id,
               CAST(1 + hash(i, 'l_cu') % {customers} AS BIGINT) AS customer_id,
               <product> AS product,
               CAST(round(lognorm(i, 'l_pr', 11.0, 0.9), 2) AS DECIMAL(18, 2)) AS principal,
               round(0.18 + u(i, 'l_ir') * 0.42, 4) AS interest_rate,
               DATE '2021-01-01' + CAST(floor(u(i, 'l_sd') * 2100) AS INTEGER) AS start_date,
               CAST(pick(i, 'l_tm', [12, 24, 36, 48, 60, 120]) AS SMALLINT) AS term_months,
               CAST(CASE WHEN u(i, 'l_dpd') < 0.86 THEN 0
                         WHEN u(i, 'l_dpd') < 0.94 THEN 1 + floor(u(i, 'l_dp2') * 89)
                         ELSE 90 + floor(u(i, 'l_dp2') * 400) END AS INTEGER) AS days_past_due,
               <status> AS status
        FROM range(1, {n} + 1) t(i)
        """,
        product=weighted(
            "u(i, 'l_pd')", [("consumer", 0.55), ("mortgage", 0.18), ("auto", 0.15), ("sme", 0.12)]
        ),
        status=weighted(
            "u(i, 'l_st')", [("active", 0.78), ("closed", 0.20), ("written_off", 0.02)]
        ),
    ),
)

# Accounts under review. Small on purpose: it is the kind of table whose keys
# are handed to the source to fetch a sliver of a very large one.
WATCHLIST = Table(
    name="watchlist",
    base=5_000,
    floor=40,
    primary_key=("watch_id",),
    indexes=(("account_id",),),
    select=tpl(
        """
        SELECT CAST(i AS INTEGER) AS watch_id,
               CAST(1 + hash(i, 'w_acc') % {accounts} AS BIGINT) AS account_id,
               <reason> AS reason,
               day_at(i, 'w_on') AS listed_on,
               pick(i, 'w_an', <first>) AS analyst
        FROM range(1, {n} + 1) t(i)
        """,
        first=sql_list(FIRST_NAMES),
        reason=weighted(
            "u(i, 'w_r')",
            [("iç denetim", 0.40), ("MASAK talebi", 0.30), ("PEP", 0.20), ("yaptırım", 0.10)],
        ),
    ),
)

MERCHANT_KINDS = [
    "Market", "Akaryakıt", "Eczane", "Lokanta", "Giyim", "Elektronik", "Kırtasiye", "Çiçekçi",
]  # fmt: skip

MERCHANTS = Table(
    name="merchants",
    base=20_000,
    floor=200,
    primary_key=("merchant_id",),
    select=tpl(
        """
        SELECT CAST(i AS INTEGER) AS merchant_id,
               kind || ' ' || pick(i, 'm_ln', <last>) || ' ' || CAST(i AS VARCHAR)
                   AS merchant_name,
               CAST(pick(i, 'm_mcc', [5411, 5541, 5912, 5812, 5651, 5732, 5943, 5992])
                    AS INTEGER) AS mcc,
               kind AS category,
               <city> AS city,
               <country> AS country
        FROM (SELECT i, pick(i, 'm_kind', <kinds>) AS kind FROM range(1, {n} + 1) t(i))
        """,
        last=sql_list(LAST_NAMES),
        kinds=sql_list(MERCHANT_KINDS),
        city=city("i", "m_city"),
        country=weighted(
            "u(i, 'm_co')", [("TR", 0.96), ("DE", 0.01), ("US", 0.01), ("GB", 0.01), ("NL", 0.01)]
        ),
    ),
)

CARDS = Table(
    name="cards",
    base=300_000,
    floor=800,
    primary_key=("card_id",),
    indexes=(("customer_id",),),
    select=tpl(
        """
        SELECT CAST(i AS BIGINT) AS card_id,
               CAST(1 + hash(i, 'k_cu') % {bank_core__customers} AS BIGINT) AS customer_id,
               <kind> AS card_type,
               CAST(pick(i, 'k_lim', [5000, 10000, 25000, 50000, 100000, 250000])
                    AS DECIMAL(12, 2)) AS card_limit,
               <status> AS status,
               DATE '2019-01-01' + CAST(floor(u(i, 'k_is') * 2800) AS INTEGER) AS issued_on
        FROM range(1, {n} + 1) t(i)
        """,
        kind=weighted("u(i, 'k_ty')", [("credit", 0.55), ("debit", 0.38), ("virtual", 0.07)]),
        status=weighted("u(i, 'k_st')", [("active", 0.90), ("expired", 0.06), ("blocked", 0.04)]),
    ),
)

CARD_TRANSACTIONS = Table(
    name="card_transactions",
    base=4_000_000,
    floor=16_000,
    primary_key=("ctx_id",),
    indexes=(("card_id",), ("merchant_id",), ("auth_ts",)),
    description="Card authorizations; auth_ts is zoned and fraud_score is single precision.",
    select=tpl(
        """
        SELECT CAST(i AS BIGINT) AS ctx_id,
               skew(i, 'ct_card', {cards}, 1.6) AS card_id,
               CAST(skew(i, 'ct_mer', {merchants}, 2.2) AS INTEGER) AS merchant_id,
               CAST(ts_at(i, 'ct_ts') AS TIMESTAMP WITH TIME ZONE) AS auth_ts,
               CAST(round(lognorm(i, 'ct_amt', 4.6, 1.2), 2) AS DECIMAL(12, 2)) AS amount,
               <currency> AS currency,
               u(i, 'ct_ec') < 0.35 AS is_ecommerce,
               u(i, 'ct_cl') < 0.50 AS is_contactless,
               <response> AS response_code,
               CAST(pick(i, 'ct_in', [1, 1, 1, 1, 3, 6, 9, 12]) AS SMALLINT) AS installment_count,
               CAST(pow(u(i, 'ct_fs'), 6) AS REAL) AS fraud_score
        FROM range(1, {n} + 1) t(i)
        """,
        currency=weighted("u(i, 'ct_ccy')", [("TRY", 0.92), ("EUR", 0.05), ("USD", 0.03)]),
        response=weighted(
            "u(i, 'ct_rc')", [("00", 0.94), ("51", 0.03), ("05", 0.02), ("91", 0.01)]
        ),
    ),
)

FRAUD_ALERTS = Table(
    name="fraud_alerts",
    base=40_000,
    floor=300,
    primary_key=("alert_id",),
    indexes=(("ctx_id",),),
    select=tpl(
        """
        SELECT CAST(i AS BIGINT) AS alert_id,
               ctx_id,
               pick(i, 'f_rule', ['VELOCITY', 'GEO_MISMATCH', 'HIGH_AMOUNT', 'MCC_RISK',
                                  'NIGHT_ECOM', 'CARD_TESTING']) AS rule_code,
               <severity> AS severity,
               CAST(ts_at(ctx_id, 'ct_ts') AS TIMESTAMP WITH TIME ZONE)
                   + to_minutes(CAST(1 + hash(i, 'f_lag') % 180 AS BIGINT)) AS created_at,
               resolved,
               CASE WHEN resolved THEN pick(i, 'f_an', <first>) END AS analyst
        FROM (
            SELECT i,
                   CAST(1 + hash(i, 'f_ctx') % {card_transactions} AS BIGINT) AS ctx_id,
                   u(i, 'f_res') < 0.70 AS resolved
            FROM range(1, {n} + 1) t(i)
        )
        """,
        first=sql_list(FIRST_NAMES),
        severity=weighted(
            "u(i, 'f_sev')", [("low", 0.45), ("medium", 0.33), ("high", 0.17), ("critical", 0.05)]
        ),
    ),
)

DATABASES = (
    Database(
        name="bank_core",
        sector="banking",
        engine="postgres",
        pushdown=False,
        description="Core banking: customers, accounts, movements, loans.",
        tables=(BRANCHES, CUSTOMERS, ACCOUNTS, TRANSACTIONS, LOANS, WATCHLIST),
    ),
    Database(
        name="bank_cards",
        sector="banking",
        engine="mysql",
        pushdown=True,
        description="Card platform: cards, merchants, authorizations, fraud alerts.",
        tables=(MERCHANTS, CARDS, CARD_TRANSACTIONS, FRAUD_ALERTS),
    ),
)
