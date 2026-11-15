"""Retail: a store POS database and an e-commerce database.

``retail_pos`` answers SQL (pushdown); ``retail_ecom`` does not. A product's
price is a function of its id, so the POS lines, the product master and the
web orders agree without ever being joined during generation.
"""

from __future__ import annotations

from scenarios.synth.core import Database, Table, city, region_of, sql_list, tpl, weighted

CATEGORIES = [
    "Gıda", "İçecek", "Temizlik", "Kişisel Bakım", "Süt Ürünleri", "Atıştırmalık",
    "Bebek", "Ev Gereçleri", "Elektronik", "Kırtasiye", "Dondurulmuş", "Şarküteri",
]  # fmt: skip
PRODUCT_NAMES = [
    "Çikolata", "Süt", "Zeytinyağı", "Şampuan", "Deterjan", "Çay", "Kahve", "Makarna",
    "Peynir", "Yoğurt", "Bisküvi", "Meyve Suyu", "Un", "Şeker", "Pirinç", "Tıraş Köpüğü",
]  # fmt: skip
BRANDS = ["Anadolu", "Çınar", "Güneş", "Yıldız", "Ege", "Doğa", "Öz", "Şölen", "Marmara", "Ilgaz"]


def price(product_id: str) -> str:
    """List price of a product, from its id alone."""
    return f"round(4.9 + 495 * pow(u({product_id}, 'p_price'), 3), 2)"


STORES = Table(
    name="stores",
    base=400,
    floor=20,
    primary_key=("store_id",),
    select=tpl(
        """
        SELECT CAST(i AS INTEGER) AS store_id,
               'Mağaza ' || c || ' ' || CAST(i AS VARCHAR) AS store_name,
               c AS city,
               <region> AS region,
               <format> AS format,
               CAST(120 + floor(pow(u(i, 's_sqm'), 2) * 6000) AS INTEGER) AS sqm,
               DATE '2005-01-01' + CAST(floor(u(i, 's_op') * 7500) AS INTEGER) AS opened_on
        FROM (SELECT i, <city> AS c FROM range(1, {n} + 1) t(i))
        """,
        city=city("i", "s_city"),
        region=region_of("c"),
        format=weighted(
            "u(i, 's_fmt')",
            [("express", 0.50), ("super", 0.35), ("hyper", 0.10), ("dark_store", 0.05)],
        ),
    ),
)

PRODUCTS = Table(
    name="products",
    base=30_000,
    floor=500,
    primary_key=("product_id",),
    indexes=(("category",),),
    select=tpl(
        """
        SELECT CAST(i AS INTEGER) AS product_id,
               'SKU-' || lpad(CAST(i AS VARCHAR), 7, '0') AS sku,
               pick(i, 'p_nm', <names>) || ' ' || pick(i, 'p_br', <brands>) || ' '
                   || CAST(100 + i % 900 AS VARCHAR) || 'g' AS product_name,
               pick(i, 'p_cat', <categories>) AS category,
               pick(i, 'p_br', <brands>) AS brand,
               CAST(round(<price> * (0.45 + 0.35 * u(i, 'p_cost')), 2) AS DECIMAL(10, 2))
                   AS unit_cost,
               CAST(<price> AS DECIMAL(10, 2)) AS list_price,
               u(i, 'p_pl') < 0.12 AS is_private_label
        FROM range(1, {n} + 1) t(i)
        """,
        names=sql_list(PRODUCT_NAMES),
        brands=sql_list(BRANDS),
        categories=sql_list(CATEGORIES),
        price=price("i"),
    ),
)

# Three or four lines share a receipt; the store, the time and the loyalty
# card belong to the receipt, the product and the quantity to the line.
SALES = Table(
    name="sales",
    base=6_000_000,
    floor=24_000,
    primary_key=("sale_id",),
    indexes=(("sale_date",), ("store_id",), ("product_id",), ("receipt_id",)),
    description="POS receipt lines.",
    select=tpl(
        """
        SELECT sale_id,
               receipt_id,
               store_id,
               product_id,
               sold_at,
               CAST(sold_at AS DATE) AS sale_date,
               CAST(1 + floor(pow(u(sale_id, 's_q'), 3) * 6) AS SMALLINT) AS quantity,
               CAST(<price> AS DECIMAL(10, 2)) AS unit_price,
               CAST(pick(sale_id, 's_d', [0, 0, 0, 0, 0, 0, 5, 10, 20, 30]) AS REAL)
                   AS discount_pct,
               CASE WHEN u(receipt_id, 's_loy') < 0.40 THEN NULL
                    ELSE CAST(1 + hash(receipt_id, 's_lid') % ({retail_ecom__web_orders} // 2)
                              AS BIGINT) END AS loyalty_id,
               <payment> AS payment_method
        FROM (
            SELECT CAST(i AS BIGINT) AS sale_id,
                   r AS receipt_id,
                   CAST(skew(r, 's_st', {stores}, 1.4) AS INTEGER) AS store_id,
                   CAST(skew(i, 's_pr', {products}, 2.5) AS INTEGER) AS product_id,
                   ts_at(r, 's_ts') AS sold_at
            FROM (SELECT i, CAST(floor(i / 3.4) AS BIGINT) + 1 AS r FROM range(1, {n} + 1) t(i))
        )
        """,
        price=price("product_id"),
        payment=weighted(
            "u(receipt_id, 's_pay')",
            [("card", 0.62), ("cash", 0.25), ("mobile", 0.10), ("voucher", 0.03)],
        ),
    ),
)

INVENTORY = Table(
    name="inventory",
    base=1_000_000,
    floor=8_000,
    primary_key=("inventory_id",),
    indexes=(("store_id",), ("product_id",)),
    select="""
        SELECT CAST(i AS BIGINT) AS inventory_id,
               CAST(1 + (i - 1) % {stores} AS INTEGER) AS store_id,
               CAST(1 + ((i - 1) // {stores}) % {products} AS INTEGER) AS product_id,
               CAST(floor(pow(u(i, 'i_oh'), 2) * 400) AS INTEGER) AS on_hand,
               CAST(10 + hash(i, 'i_rp') % 60 AS INTEGER) AS reorder_point,
               DATE '2026-10-31' - CAST(hash(i, 'i_lc') % 30 AS INTEGER) AS last_counted
        FROM range(1, {n} + 1) t(i)
    """,
)

PROMOTIONS = Table(
    name="promotions",
    base=2_000,
    floor=60,
    primary_key=("promo_id",),
    select="""
        SELECT CAST(i AS INTEGER) AS promo_id,
               CAST(skew(i, 'pr_p', {products}, 2.5) AS INTEGER) AS product_id,
               day_at(i, 'pr_s') AS start_date,
               day_at(i, 'pr_s') + CAST(7 + hash(i, 'pr_len') % 15 AS INTEGER) AS end_date,
               CAST(pick(i, 'pr_d', [10, 15, 20, 25, 30, 40]) AS REAL) AS discount_pct,
               pick(i, 'pr_t', ['insert', 'tv', 'app', 'kasa önü']) AS promo_type
        FROM range(1, {n} + 1) t(i)
    """,
)

WEB_ORDERS = Table(
    name="web_orders",
    base=800_000,
    floor=7_000,
    primary_key=("order_id",),
    indexes=(("product_id",), ("loyalty_id",), ("ordered_at",)),
    select=tpl(
        """
        SELECT order_id,
               CAST(1 + hash(order_id, 'wo_l') % ({n} // 2) AS BIGINT) AS loyalty_id,
               product_id,
               CAST(ts_at(order_id, 'wo_ts') AS TIMESTAMP WITH TIME ZONE) AS ordered_at,
               quantity,
               CAST(quantity * <price> AS DECIMAL(12, 2)) AS amount,
               <status> AS status,
               <city> AS delivery_city,
               <device> AS device
        FROM (
            SELECT CAST(i AS BIGINT) AS order_id,
                   CAST(skew(i, 'wo_p', {retail_pos__products}, 2.5) AS INTEGER) AS product_id,
                   CAST(1 + floor(pow(u(i, 'wo_q'), 2) * 4) AS SMALLINT) AS quantity
            FROM range(1, {n} + 1) t(i)
        )
        """,
        price=price("product_id"),
        city=city("order_id", "wo_city"),
        status=weighted(
            "u(order_id, 'wo_st')",
            [("delivered", 0.82), ("returned", 0.07), ("cancelled", 0.06), ("in_transit", 0.05)],
        ),
        device=weighted(
            "u(order_id, 'wo_dev')", [("mobile", 0.68), ("desktop", 0.27), ("tablet", 0.05)]
        ),
    ),
)

WEB_SESSIONS = Table(
    name="web_sessions",
    base=3_000_000,
    floor=12_000,
    primary_key=("session_id",),
    indexes=(("started_at",),),
    description="Clickstream sessions keyed by a UUID.",
    select=tpl(
        """
        SELECT uuid_of(i, 'ws') AS session_id,
               CASE WHEN u(i, 'ws_an') < 0.60 THEN NULL
                    ELSE CAST(1 + hash(i, 'ws_l') % ({web_orders} // 2) AS BIGINT) END
                   AS loyalty_id,
               ts_at(i, 'ws_ts') AS started_at,
               CAST(floor(lognorm(i, 'ws_du', 4.8, 1.1)) AS INTEGER) AS duration_sec,
               CAST(1 + floor(pow(u(i, 'ws_pg'), 3) * 30) AS SMALLINT) AS pages,
               <device> AS device,
               CASE WHEN u(i, 'ws_ud') < 0.30 THEN NULL
                    ELSE pick(i, 'ws_utm', ['google', 'instagram', 'eposta', 'tiktok', 'ortak'])
               END AS utm_source,
               u(i, 'ws_cv') < 0.031 AS converted,
               pick(i, 'ws_cat', <categories>) AS landing_category
        FROM range(1, {n} + 1) t(i)
        """,
        categories=sql_list(CATEGORIES),
        device=weighted("u(i, 'ws_dev')", [("mobile", 0.70), ("desktop", 0.25), ("tablet", 0.05)]),
    ),
)

RETURNS = Table(
    name="returns",
    base=60_000,
    floor=400,
    primary_key=("return_id",),
    indexes=(("order_id",),),
    select="""
        SELECT CAST(i AS BIGINT) AS return_id,
               CAST(1 + hash(i, 'rt_o') % {web_orders} AS BIGINT) AS order_id,
               pick(i, 'rt_r', ['hasarlı', 'yanlış ürün', 'beğenmedim', 'geç teslim', 'ayıplı'])
                   AS reason,
               CAST(round(5 + 400 * pow(u(i, 'rt_a'), 3), 2) AS DECIMAL(12, 2)) AS refunded_amount,
               ts_at(i, 'rt_ts') AS returned_at
        FROM range(1, {n} + 1) t(i)
    """,
)

DATABASES = (
    Database(
        name="retail_pos",
        sector="retail",
        engine="postgres",
        pushdown=True,
        description="Store operations: stores, products, receipt lines, stock, promotions.",
        tables=(STORES, PRODUCTS, SALES, INVENTORY, PROMOTIONS),
    ),
    Database(
        name="retail_ecom",
        sector="retail",
        engine="mysql",
        pushdown=False,
        description="E-commerce: orders, sessions, returns.",
        tables=(WEB_ORDERS, WEB_SESSIONS, RETURNS),
    ),
)
