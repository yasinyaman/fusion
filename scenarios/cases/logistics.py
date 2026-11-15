"""Logistics: a parcel network on MySQL, no raw SQL."""

from __future__ import annotations

from scenarios.harness import Ctx, scenario

SHIPMENTS = "logistics.shipments"
EVENTS = "logistics.tracking_events"


@scenario(
    "L01",
    sector="logistics",
    title="SLA performance by service and origin city for one month",
    sources=("logistics",),
    capabilities=("slice", "self-join-small", "nulls", "percentile", "semantic"),
)
def sla(ctx: Ctx) -> None:
    """Operations reviews October: late share, undelivered parcels and the 90th
    percentile transit time, per service and origin. Hubs are read twice (origin and
    destination), which is fine for a small table loaded whole."""
    ctx.sql(
        "SELECT s.service, o.city AS origin, COUNT(*) AS shipments, "
        "COUNT(*) FILTER (WHERE s.delivered_at IS NULL) AS undelivered, "
        "AVG(CASE WHEN s.delivered_at > s.promised_at THEN 1.0 ELSE 0.0 END) AS late_share, "
        "quantile_cont(epoch(s.delivered_at - s.created_at) / 3600.0, 0.9) AS p90_hours "
        f"FROM {SHIPMENTS} s JOIN logistics.hubs o ON o.hub_id = s.origin_hub_id "
        "JOIN logistics.hubs d ON d.hub_id = s.dest_hub_id "
        "WHERE s.created_at >= '2026-10-01' AND s.created_at < '2026-11-01' AND d.city != o.city "
        "GROUP BY s.service, o.city ORDER BY s.service, o.city",
        "October SLA per service and origin (inter-city only)",
        ordered=True,
        tolerance=1e-9,
        actions={SHIPMENTS: "load_slice"},
        max_moved=ctx.rows(SHIPMENTS) // 10 + 1_000,
    )
    ctx.tool_rows(
        "query_metrics",
        {
            "table": SHIPMENTS,
            "metrics": [
                "declared_value:weighted_avg(weight=weight_kg)",
                "weight_kg:sum",
                "*:count",
            ],
            "dimensions": ["service"],
            "filters": [
                {"column": "created_at", "op": "gte", "value": "2026-10-01"},
                {"column": "status", "op": "in", "value": ["exception", "lost"]},
            ],
        },
        f"""SELECT service, SUM(declared_value * weight_kg) / NULLIF(SUM(weight_kg), 0),
                   SUM(weight_kg), COUNT(*) FROM {SHIPMENTS}
            WHERE created_at >= '2026-10-01' AND status IN ('exception', 'lost')
            GROUP BY 1 ORDER BY 1""",
        "value at risk in lost and exception parcels",
        ordered=True,
        tolerance=1e-9,
    )


@scenario(
    "L02",
    sector="logistics",
    title="Where is every parcel created in the last two days?",
    sources=("logistics",),
    capabilities=("semi-join", "arg_max", "having"),
)
def last_scan(ctx: Ctx) -> None:
    """Customer service wants the latest scan of every recent parcel. Seven million
    tracking events are fetched only for the parcels of the last two days, by key."""

    def latest(extra: str = "") -> str:
        return (
            "SELECT s.service, COUNT(*) AS scans, COUNT(DISTINCT s.shipment_id) AS parcels, "
            "MAX(e.event_ts) AS last_seen "
            f"FROM {SHIPMENTS} s JOIN {EVENTS} e ON e.shipment_id = s.shipment_id "
            f"WHERE s.created_at >= '2026-10-29' {extra} GROUP BY s.service ORDER BY s.service"
        )

    ctx.sql(
        latest(),
        "scans of the last two days' parcels, by service",
        ordered=True,
        actions={EVENTS: "semi_join"},
        max_moved=ctx.rows(EVENTS) // 50 + 10_000,
    )
    ctx.sql(
        "SELECT s.shipment_id, s.service, arg_max(e.event_type, e.event_seq) AS last_event, "
        f"MAX(e.event_ts) AS last_seen FROM {SHIPMENTS} s JOIN {EVENTS} e "
        "ON e.shipment_id = s.shipment_id WHERE s.created_at >= '2026-10-29' "
        "GROUP BY s.shipment_id, s.service "
        "HAVING arg_max(e.event_type, e.event_seq) NOT IN ('delivered', 'out_for_delivery') "
        "ORDER BY last_seen, s.shipment_id LIMIT 100",
        "parcels whose last scan is neither delivery nor out for delivery",
        ordered=True,
    )
    ctx.sql(latest("AND s.service = 'same_day'"), "same-day only: a narrower driver", ordered=True)


@scenario(
    "L03",
    sector="logistics",
    title="Point lookups by tracking number (UUID)",
    sources=("logistics",),
    capabilities=("uuid", "point-lookup", "tools", "limit"),
)
def lookups(ctx: Ctx) -> None:
    """A call-centre agent pastes a tracking number. A UUID equality is sent to the
    source, so a lookup in a million-row table moves one row; the same UUID in upper
    case must find the same parcel."""
    rows = ctx.oracle(
        f"SELECT CAST(tracking_uuid AS VARCHAR) FROM {SHIPMENTS} "
        "WHERE shipment_id IN (17, 4242, 99) ORDER BY shipment_id"
    )
    uuids = [r[0] for r in rows]
    one = uuids[0]
    query = (
        f"SELECT shipment_id, service, status, delivered_at FROM {SHIPMENTS} "
        f"WHERE tracking_uuid = '{one}'"
    )
    ctx.sql(query, "lookup by tracking UUID", actions={SHIPMENTS: "load_slice"}, max_moved=1)
    shouted = query.replace(one, one.upper())
    if ctx.inproc:
        ctx.sql(shouted, "the same UUID in upper case", max_moved=1)
    else:
        # MySQL has no UUID type: the fixture stores CHAR(36), which its default
        # collation compares case-insensitively. Fusion applies the query's own
        # = again over what came back, as DuckDB would, so the row is not found.
        seen = len(ctx.app.query.sql(shouted, use_cache=False).rows)
        ctx.note(
            "upper-case UUID on a MySQL CHAR(36) column",
            f"{seen} row(s) (the source matches case-insensitively, Fusion's own = does not)",
        )
    listed = ", ".join(f"'{u}'" for u in uuids)
    ctx.sql(
        f"SELECT shipment_id, status FROM {SHIPMENTS} WHERE tracking_uuid IN ({listed})",
        "three UUIDs at once",
        max_moved=3,
    )
    ctx.limit(
        f"SELECT COUNT(*) FROM {SHIPMENTS} WHERE CAST(tracking_uuid AS VARCHAR) LIKE '{one[:8]}%'",
        "a prefix search on a UUID (LIKE is not sent for uuid)",
    )
    if ctx.inproc:
        ctx.fails(
            f"SELECT * FROM {SHIPMENTS} WHERE tracking_uuid = 'not-a-uuid'",
            "a malformed UUID is an error, not a full-table load",
            contains="",
        )
    else:
        ctx.sql(
            f"SELECT COUNT(*) FROM {SHIPMENTS} WHERE tracking_uuid = 'not-a-uuid'",
            "a malformed UUID on CHAR(36) is just a text that matches nothing",
            oracle_sql=f"SELECT COUNT(*) FROM {SHIPMENTS} "
            "WHERE CAST(tracking_uuid AS VARCHAR) = 'not-a-uuid'",
        )
    found = ctx.app.tools.execute(
        "search_data", {"table": SHIPMENTS, "filter_column": "tracking_uuid", "filter_value": one}
    )
    ctx.note(
        "search_data on a UUID column (only text columns are searched as slices)",
        str(found.get("error", f"{found.get('row_count')} row(s)")).splitlines()[0],
    )


@scenario(
    "L04",
    sector="logistics",
    title="A gateway outage and a gateway that caps its reads",
    sources=("logistics",),
    capabilities=("outage", "export-cap", "partial-slice", "recovery"),
    modes=("inproc",),
)
def outage(ctx: Ctx) -> None:
    """The gateway goes away mid-shift: what is already loaded keeps answering, a new
    load fails with a clear error, and the next query after it returns works. A second
    gateway caps every read: a whole-table load it cuts short must be refused, and a
    cut-short slice must never be passed off as the full answer."""
    hubs = "SELECT hub_type, COUNT(*) AS n FROM logistics.hubs GROUP BY hub_type ORDER BY hub_type"
    ctx.sql(hubs, "hubs loaded while the gateway is up", ordered=True)
    source = ctx.source("logistics")
    assert source is not None
    source.down = True
    ctx.sql(
        hubs.replace("COUNT(*)", "COUNT(hub_id)"),
        "loaded data still answers in an outage",
        ordered=True,
        oracle_sql=hubs,
    )
    ctx.fails(
        "SELECT vehicle_type, COUNT(*) FROM logistics.vehicles GROUP BY 1",
        "a new load fails with a clear error",
        contains="unreachable",
    )
    tools = ctx.tool("list_sources", {}, "list_sources still answers")
    ctx.check("list_sources reports the source", "logistics" in str(tools))
    source.down = False
    ctx.sql(
        "SELECT vehicle_type, COUNT(*) AS n FROM logistics.vehicles GROUP BY 1 ORDER BY 1",
        "after recovery the same load works",
        ordered=True,
    )

    cap = max(10, ctx.rows("logistics.vehicles") // 5)
    ctx.attach("capped", "logistics", export_max_rows=cap)
    ctx.fails(
        "SELECT COUNT(*) FROM capped.vehicles",
        f"a whole-table load cut at {cap} rows is refused",
        contains="export limit",
    )
    trucks = "SELECT COUNT(*) AS n FROM capped.vehicles WHERE vehicle_type = 'kamyon'"
    want = ctx.scalar("SELECT count(*) FROM logistics.vehicles WHERE vehicle_type = 'kamyon'")
    result = None
    try:
        result = ctx.app.query.sql(trucks, use_cache=False)
    except Exception as e:
        ctx.check(
            "a capped slice is refused or answered right",
            "export" in str(e).lower(),
            str(e).splitlines()[0],
        )
    if result is not None:
        got = result.rows[0][0]
        ctx.check(
            "a slice the source cut short does not pass for the full answer",
            got == want,
            f"fusion says {got}, the source has {want} (cap {cap})",
        )
    held = [s for s in ctx.app.catalog.slices_of("capped.vehicles")]
    ctx.check(
        "the cut-short slice is marked incomplete",
        all(not s.complete for s in held if s.row_count >= cap),
        str([(s.row_count, s.complete) for s in held]),
    )
