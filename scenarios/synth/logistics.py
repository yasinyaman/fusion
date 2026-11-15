"""Logistics: a parcel network on MySQL.

Every shipment has the same number of tracking events (give or take one), in
sequence, so "the last event of each shipment" and "time between two scans"
are well defined.
"""

from __future__ import annotations

from scenarios.synth.core import CITIES, Database, Table, sql_list, tpl, weighted

HUBS = Table(
    name="hubs",
    base=120,
    floor=24,
    primary_key=("hub_id",),
    select=tpl(
        """
        SELECT CAST(i AS INTEGER) AS hub_id,
               c || ' Aktarma ' || CAST(i AS VARCHAR) AS hub_name,
               c AS city,
               round(36 + 6 * u(i, 'h_lat'), 5) AS lat,
               round(26 + 19 * u(i, 'h_lon'), 5) AS lon,
               <kind> AS hub_type
        FROM (SELECT i, <cities>[1 + (i - 1) % <city_count>] AS c FROM range(1, {n} + 1) t(i))
        """,
        cities=sql_list(CITIES),
        city_count=str(len(CITIES)),
        kind=weighted("u(i, 'h_ty')", [("aktarma", 0.60), ("şube", 0.30), ("liman", 0.10)]),
    ),
)

VEHICLES = Table(
    name="vehicles",
    base=4_000,
    floor=80,
    primary_key=("vehicle_id",),
    select=tpl(
        """
        SELECT CAST(i AS INTEGER) AS vehicle_id,
               lpad(CAST(1 + hash(i, 'vh_il') % 81 AS VARCHAR), 2, '0') || ' '
                   || chr(CAST(65 + hash(i, 'vh_a') % 26 AS INTEGER))
                   || chr(CAST(65 + hash(i, 'vh_b') % 26 AS INTEGER)) || ' '
                   || CAST(100 + hash(i, 'vh_n') % 900 AS VARCHAR) AS plate,
               <kind> AS vehicle_type,
               CAST(pick(i, 'vh_cap', [150, 800, 3500, 12000, 24000]) AS INTEGER) AS capacity_kg,
               CAST(1 + hash(i, 'vh_hub') % {hubs} AS INTEGER) AS home_hub_id,
               CAST(2008 + hash(i, 'vh_my') % 18 AS SMALLINT) AS model_year
        FROM range(1, {n} + 1) t(i)
        """,
        kind=weighted(
            "u(i, 'vh_ty')",
            [("kamyonet", 0.50), ("kamyon", 0.30), ("tır", 0.15), ("motosiklet", 0.05)],
        ),
    ),
)

SHIPMENTS = Table(
    name="shipments",
    base=1_500_000,
    floor=6_000,
    primary_key=("shipment_id",),
    indexes=(("created_at",), ("origin_hub_id",), ("dest_hub_id",), ("tracking_uuid",)),
    description="Parcels; 4% are never delivered and have no delivery time.",
    select=tpl(
        """
        SELECT shipment_id,
               uuid_of(shipment_id, 'sh') AS tracking_uuid,
               CAST(skew(shipment_id, 'sh_o', {hubs}, 1.5) AS INTEGER) AS origin_hub_id,
               CAST(1 + hash(shipment_id, 'sh_d') % {hubs} AS INTEGER) AS dest_hub_id,
               CAST(1 + hash(shipment_id, 'sh_v') % {vehicles} AS INTEGER) AS vehicle_id,
               'MŞT-' || lpad(CAST(1 + hash(shipment_id, 'sh_cu') % 50000 AS VARCHAR), 6, '0')
                   AS customer_ref,
               service,
               CAST(round(lognorm(shipment_id, 'sh_w', 1.6, 1.1), 2) AS DECIMAL(8, 2))
                   AS weight_kg,
               CAST(round(lognorm(shipment_id, 'sh_val', 6.5, 1.3), 2) AS DECIMAL(12, 2))
                   AS declared_value,
               created_at,
               created_at + to_hours(sla_hours) AS promised_at,
               CASE WHEN lost THEN NULL
                    ELSE created_at + to_minutes(CAST(floor(
                        sla_hours * 60 * (0.55 + 0.75 * pow(u(shipment_id, 'sh_late'), 1.5)))
                        AS BIGINT)) END AS delivered_at,
               CASE WHEN lost THEN pick(shipment_id, 'sh_st', ['in_transit', 'exception', 'lost'])
                    ELSE 'delivered' END AS status,
               round(20 + 1500 * u(shipment_id, 'sh_km'), 1) AS distance_km
        FROM (
            SELECT shipment_id, service, created_at,
                   CAST(CASE service WHEN 'same_day' THEN 12 WHEN 'express' THEN 30
                                     WHEN 'standard' THEN 72 ELSE 120 END AS BIGINT) AS sla_hours,
                   u(shipment_id, 'sh_lost') < 0.04 AS lost
            FROM (
                SELECT CAST(i AS BIGINT) AS shipment_id,
                       <service> AS service,
                       ts_at(i, 'sh_ts') AS created_at
                FROM range(1, {n} + 1) t(i)
            )
        )
        """,
        service=weighted(
            "u(i, 'sh_sv')",
            [("standard", 0.62), ("express", 0.24), ("same_day", 0.08), ("freight", 0.06)],
        ),
    ),
)

TRACKING_EVENTS = Table(
    name="tracking_events",
    base=7_000_000,
    floor=28_000,
    primary_key=("event_id",),
    indexes=(("shipment_id",), ("event_ts",), ("hub_id",)),
    select="""
        SELECT CAST(i AS BIGINT) AS event_id,
               shipment_id,
               CAST(seq AS SMALLINT) AS event_seq,
               CAST(1 + hash(shipment_id, 'te_h' || CAST(seq AS VARCHAR)) % {hubs} AS INTEGER)
                   AS hub_id,
               CASE WHEN u(i, 'te_ex') < 0.02 THEN 'exception'
                    ELSE ['picked_up', 'arrived_hub', 'departed_hub', 'arrived_hub',
                          'out_for_delivery', 'delivered'][1 + least(seq, 5)] END AS event_type,
               ts_at(shipment_id, 'sh_ts')
                   + to_minutes(CAST(seq * 240 + floor(u(i, 'te_gap') * 200) AS BIGINT))
                   AS event_ts
        FROM (
            SELECT i,
                   CAST(1 + (i - 1) % {shipments} AS BIGINT) AS shipment_id,
                   (i - 1) // {shipments} AS seq
            FROM range(1, {n} + 1) t(i)
        )
    """,
)

DATABASES = (
    Database(
        name="logistics",
        sector="logistics",
        engine="mysql",
        pushdown=False,
        description="Parcel network: hubs, vehicles, shipments, tracking scans.",
        tables=(HUBS, VEHICLES, SHIPMENTS, TRACKING_EVENTS),
    ),
)
