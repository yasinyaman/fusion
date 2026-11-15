"""Deterministic data synthesis on DuckDB.

Every value is a pure function of the row number and a salt (``hash(i, salt)``),
never of ``random()``: the same scale always produces the same rows whatever
the thread count, so a result that changes between two runs is the engine's
doing and not the data's.

A *world* is one DuckDB connection with every source database attached as its
own catalog. The in-process data sources and the oracle both read from it,
which is what lets the harness compare Fusion's answer to the truth.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

#: The data covers [START, START + DAYS): two years ending 2026-10-31.
START = "2024-11-01"
DAYS = 730
END = "2026-10-31"

MACROS = """
CREATE OR REPLACE MACRO u(i, salt) AS (hash(i, salt) % 1000003) / 1000003.0;
CREATE OR REPLACE MACRO pick(i, salt, xs) AS xs[1 + CAST(hash(i, salt) % len(xs) AS BIGINT)];
CREATE OR REPLACE MACRO skew(i, salt, n, k) AS
    1 + CAST(least(n - 1, floor(n * pow(u(i, salt), k))) AS BIGINT);
CREATE OR REPLACE MACRO gauss(i, salt) AS
    sqrt(-2 * ln(greatest(u(i, salt), 1e-9))) * cos(2 * pi() * u(i, salt || '#'));
CREATE OR REPLACE MACRO lognorm(i, salt, mu, sigma) AS exp(mu + sigma * gauss(i, salt));
CREATE OR REPLACE MACRO ts_at(i, salt) AS
    TIMESTAMP '2024-11-01 00:00:00' + to_seconds(CAST(floor(u(i, salt) * 730 * 86400) AS BIGINT));
CREATE OR REPLACE MACRO day_at(i, salt) AS
    DATE '2024-11-01' + CAST(floor(u(i, salt) * 730) AS INTEGER);
CREATE OR REPLACE MACRO uuid_of(i, salt) AS CAST(md5(CAST(i AS VARCHAR) || salt) AS UUID);
"""

# Turkish place and person names on purpose: dotted and dotless i, cedilla and
# breve are where a source's collation and the store's start to disagree.
CITIES = [
    "İstanbul", "Ankara", "İzmir", "Bursa", "Antalya", "Adana", "Konya", "Gaziantep",
    "Şanlıurfa", "Kayseri", "Diyarbakır", "Mersin", "Eskişehir", "Samsun", "Trabzon",
    "Iğdır", "Muğla", "Çanakkale", "Kırklareli", "Ağrı",
]  # fmt: skip
REGIONS = {
    "İstanbul": "Marmara", "Bursa": "Marmara", "Çanakkale": "Marmara", "Kırklareli": "Marmara",
    "Ankara": "İç Anadolu", "Konya": "İç Anadolu", "Kayseri": "İç Anadolu",
    "Eskişehir": "İç Anadolu", "İzmir": "Ege", "Muğla": "Ege", "Antalya": "Akdeniz",
    "Adana": "Akdeniz", "Mersin": "Akdeniz", "Gaziantep": "Güneydoğu Anadolu",
    "Şanlıurfa": "Güneydoğu Anadolu", "Diyarbakır": "Güneydoğu Anadolu",
    "Samsun": "Karadeniz", "Trabzon": "Karadeniz", "Iğdır": "Doğu Anadolu",
    "Ağrı": "Doğu Anadolu",
}  # fmt: skip
FIRST_NAMES = [
    "Ayşe", "Mehmet", "Zeynep", "Çağrı", "Ömer", "İbrahim", "Gülşen", "Işıl", "Şeyma", "Uğur",
    "Fatma", "Ali", "Elif", "Mustafa", "Özge", "Emre", "Büşra", "Hüseyin", "Ceren", "Yiğit",
]  # fmt: skip
LAST_NAMES = [
    "Yılmaz", "Kaya", "Demir", "Şahin", "Çelik", "Yıldız", "Öztürk", "Aydın", "Özdemir",
    "Arslan", "Doğan", "Kılıç", "Aslan", "Çetin", "Koç", "Kurt", "Özkan", "Şimşek", "Polat",
    "Güneş",
]  # fmt: skip


def sql_list(values: Iterable[Any]) -> str:
    """A DuckDB list literal: ``['a', 'b']``."""
    return "[" + ", ".join(sql_lit(v) for v in values) + "]"


def sql_lit(value: Any) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def weighted(u_expr: str, choices: Sequence[tuple[Any, float]]) -> str:
    """A CASE that turns a uniform ``u_expr`` into a weighted choice."""
    total = sum(weight for _, weight in choices)
    parts, cumulative = [], 0.0
    for value, weight in choices[:-1]:
        cumulative += weight / total
        parts.append(f"WHEN {u_expr} < {cumulative:.6f} THEN {sql_lit(value)}")
    return f"(CASE {' '.join(parts)} ELSE {sql_lit(choices[-1][0])} END)"


def city(i: str, salt: str) -> str:
    """A city, skewed towards the head of the list as real populations are."""
    return f"{sql_list(CITIES)}[skew({i}, '{salt}', {len(CITIES)}, 2.2)]"


def region_of(city_expr: str) -> str:
    cases = " ".join(f"WHEN {sql_lit(c)} THEN {sql_lit(r)}" for c, r in REGIONS.items())
    return f"(CASE {city_expr} {cases} END)"


@dataclass(frozen=True, slots=True)
class Table:
    """One synthetic table.

    ``base`` is the row count at scale 1.0 and ``floor`` the fewest rows it
    ever has, so a tiny smoke run still has every dimension populated.
    ``select`` is a complete SELECT over ``range(1, {n} + 1) t(i)``; ``{name}``
    placeholders are filled with the sizes of the tables in the same world,
    as ``{table}`` (same database) or ``{database__table}``.
    """

    name: str
    base: int
    select: str
    floor: int = 10
    primary_key: tuple[str, ...] = ()
    indexes: tuple[tuple[str, ...], ...] = ()
    description: str = ""

    def rows(self, scale: float) -> int:
        return max(self.floor, int(self.base * scale))


@dataclass(frozen=True, slots=True)
class Database:
    """A source database: one catalog in the world, one database behind Warp."""

    name: str
    sector: str
    tables: tuple[Table, ...]
    #: Which engine it is exported to for the Warp run.
    engine: str = "postgres"
    #: Whether the source answers raw SQL (Warp's ``enable_raw_query``).
    pushdown: bool = False
    description: str = ""

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(f"{self.name}.{name}")


@dataclass(slots=True)
class World:
    """Every source database, attached to one DuckDB connection."""

    conn: duckdb.DuckDBPyConnection
    scale: float
    databases: dict[str, Database] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    path: Path | None = None

    def rows(self, table: str) -> int:
        """Source-side row count of ``database.table``."""
        return self.sizes[table]

    def cursor(self) -> duckdb.DuckDBPyConnection:
        cursor = self.conn.cursor()
        # TIMESTAMPTZ is read in the session zone; pin it so a date_trunc over
        # one means the same day here as in Fusion's store.
        cursor.execute("SET TimeZone = 'UTC'")
        return cursor

    def close(self) -> None:
        self.conn.close()


def _size_map(databases: Iterable[Database], scale: float) -> dict[str, int]:
    return {f"{db.name}.{t.name}": t.rows(scale) for db in databases for t in db.tables}


def _format_args(database: Database, sizes: Mapping[str, int]) -> dict[str, int]:
    args: dict[str, int] = {}
    for full_name, rows in sizes.items():
        db_name, table = full_name.split(".", 1)
        args[f"{db_name}__{table}"] = rows
        if db_name == database.name:
            args[table] = rows
    return args


def data_dir(root: Path, scale: float) -> Path:
    return root / f"scale-{scale:g}"


def generate(
    databases: Sequence[Database],
    scale: float,
    root: Path,
    *,
    force: bool = False,
    say: Callable[[str], None] = lambda _msg: None,
) -> Path:
    """Write one ``<database>.duckdb`` file per source database under ``root``.

    A database whose file already exists is kept unless ``force``: generation
    is deterministic, so the file on disk is the file a new run would write.
    """
    target = data_dir(root, scale)
    target.mkdir(parents=True, exist_ok=True)
    sizes = _size_map(databases, scale)
    for database in databases:
        path = target / f"{database.name}.duckdb"
        if path.exists() and not force:
            say(f"  {database.name}: kept ({path.stat().st_size / 1e6:.0f} MB)")
            continue
        path.unlink(missing_ok=True)
        started = time.perf_counter()
        conn = duckdb.connect(str(path))
        try:
            conn.execute("SET TimeZone = 'UTC'")
            conn.execute(MACROS)
            args = _format_args(database, sizes)
            total = 0
            for table in database.tables:
                n = table.rows(scale)
                select = table.select.format(n=n, **args)
                conn.execute(f'CREATE TABLE "{table.name}" AS {select}')
                total += n
            # The macros live in the file otherwise, and a reader has no use for them.
            for name in ("u", "pick", "skew", "gauss", "lognorm", "ts_at", "day_at", "uuid_of"):
                conn.execute(f"DROP MACRO IF EXISTS {name}")
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
        say(
            f"  {database.name}: {total:,} rows in {time.perf_counter() - started:.1f}s "
            f"({path.stat().st_size / 1e6:.0f} MB)"
        )
    return target


def open_world(
    databases: Sequence[Database],
    scale: float,
    root: Path,
    *,
    names: Iterable[str] | None = None,
    threads: int | None = None,
) -> World:
    """Attach the generated databases read-only to one in-memory connection."""
    target = data_dir(root, scale)
    conn = duckdb.connect(":memory:")
    if threads:
        conn.execute(f"SET threads = {int(threads)}")
    conn.execute("SET TimeZone = 'UTC'")
    wanted = set(names) if names is not None else None
    world = World(conn=conn, scale=scale, path=target)
    for database in databases:
        if wanted is not None and database.name not in wanted:
            continue
        path = target / f"{database.name}.duckdb"
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing: run `python -m scenarios generate --scale {scale:g}` first."
            )
        conn.execute(f"ATTACH '{path}' AS \"{database.name}\" (READ_ONLY)")
        world.databases[database.name] = database
        for table in database.tables:
            world.sizes[f"{database.name}.{table.name}"] = table.rows(scale)
    return world


def tpl(sql: str, **fragments: str) -> str:
    """Fill ``<name>`` tokens in a SELECT template with SQL fragments.

    Separate from the ``{table}`` size placeholders, which are filled later
    and per scale.
    """
    for key, value in fragments.items():
        sql = sql.replace(f"<{key}>", value)
    return sql
