"""Export the synthesized databases to a Postgres + MySQL + Warp stack.

The in-process run reads the DuckDB files through a stand-in for Warp. This
module writes what is needed to run the same scenarios against the real thing:

* one CSV per table (NULL spelled out, so empty text survives),
* init scripts that create each database, its tables, keys and indexes and
  bulk-load the CSVs (Postgres ``COPY``, MySQL ``LOAD DATA INFILE``),
* two Warp configs: one that refuses raw SQL (every database) and one that
  allows it (the databases marked ``pushdown``),
* a ``docker-compose.yml`` and a README with the commands.

Types are mapped per engine. Two are lossy on MySQL on purpose, because that is
what a real MySQL source looks like: a zoned timestamp becomes a UTC
``DATETIME(6)``, a UUID becomes ``CHAR(36)``, and text comparisons follow the
accent- and case-insensitive default collation.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Sequence
from pathlib import Path

import duckdb

from scenarios.synth.core import Database, data_dir

_NULL = {"postgres": r"\N", "mysql": "NULL"}


def _pg_type(duck: str) -> str:
    t = duck.upper()
    if t.startswith("DECIMAL"):
        return t.replace("DECIMAL", "NUMERIC")
    return {
        "BIGINT": "BIGINT",
        "INTEGER": "INTEGER",
        "SMALLINT": "SMALLINT",
        "DOUBLE": "DOUBLE PRECISION",
        "FLOAT": "REAL",
        "VARCHAR": "TEXT",
        "BOOLEAN": "BOOLEAN",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
        "UUID": "UUID",
    }.get(t, "TEXT")


def _mysql_type(duck: str) -> str:
    t = duck.upper()
    if t.startswith("DECIMAL"):
        return t
    return {
        "BIGINT": "BIGINT",
        "INTEGER": "INT",
        "SMALLINT": "SMALLINT",
        "DOUBLE": "DOUBLE",
        "FLOAT": "FLOAT",
        "VARCHAR": "VARCHAR(255)",
        "BOOLEAN": "TINYINT(1)",
        "DATE": "DATE",
        "TIMESTAMP": "DATETIME(6)",
        "TIMESTAMP WITH TIME ZONE": "DATETIME(6)",
        "UUID": "CHAR(36)",
    }.get(t, "TEXT")


def _export_expr(column: str, duck: str, engine: str) -> str:
    """How a column is written to CSV for ``engine``."""
    q = f'"{column}"'
    t = duck.upper()
    if engine == "mysql":
        if t == "BOOLEAN":
            return f"CAST({q} AS INTEGER) AS {q}"
        if t == "TIMESTAMP WITH TIME ZONE":
            return f"CAST({q} AS TIMESTAMP) AS {q}"  # the session zone is UTC
    return q


def _ident(name: str, engine: str) -> str:
    return f"`{name}`" if engine == "mysql" else f'"{name}"'


def export(
    databases: Sequence[Database],
    scale: float,
    root: Path,
    out: Path,
    *,
    say: Callable[[str], None] = lambda _m: None,
) -> Path:
    """Write CSVs, init scripts, Warp configs and a compose file under ``out``."""
    source_dir = data_dir(root, scale)
    out.mkdir(parents=True, exist_ok=True)
    data = out / "data"
    init = {"postgres": out / "postgres-init", "mysql": out / "mysql-init"}
    for path in (data, *init.values()):
        path.mkdir(parents=True, exist_ok=True)

    for order, database in enumerate(databases, start=1):
        engine = database.engine
        conn = duckdb.connect(str(source_dir / f"{database.name}.duckdb"), read_only=True)
        conn.execute("SET TimeZone = 'UTC'")
        (data / database.name).mkdir(exist_ok=True)
        lines: list[str] = []
        if engine == "postgres":
            lines += [f'CREATE DATABASE "{database.name}";', f"\\connect {database.name}", ""]
        else:
            lines += [
                f"CREATE DATABASE `{database.name}` CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_0900_ai_ci;",
                f"USE `{database.name}`;",
                "",
            ]
        for table in database.tables:
            columns = conn.execute(f'DESCRIBE "{table.name}"').fetchall()
            exprs = ", ".join(_export_expr(c[0], c[1], engine) for c in columns)
            csv = data / database.name / f"{table.name}.csv"
            conn.execute(
                f"COPY (SELECT {exprs} FROM \"{table.name}\") TO '{csv}' "
                f"(HEADER, DELIMITER ',', QUOTE '\"', NULLSTR '{_NULL[engine]}')"
            )
            to_type = _pg_type if engine == "postgres" else _mysql_type
            defs = [f"  {_ident(c[0], engine)} {to_type(c[1])}" for c in columns]
            if table.primary_key:
                keys = ", ".join(_ident(k, engine) for k in table.primary_key)
                defs.append(f"  PRIMARY KEY ({keys})")
            name = _ident(table.name, engine)
            lines.append(f"CREATE TABLE {name} (\n" + ",\n".join(defs) + "\n);")
            if engine == "postgres":
                lines.append(
                    f"COPY {name} FROM '/data/{database.name}/{table.name}.csv' "
                    "WITH (FORMAT csv, HEADER true, NULL '\\N');"
                )
            else:
                lines.append(
                    f"LOAD DATA INFILE '/import/{database.name}/{table.name}.csv' "
                    f"INTO TABLE {name} CHARACTER SET utf8mb4 FIELDS TERMINATED BY ',' "
                    "OPTIONALLY ENCLOSED BY '\"' ESCAPED BY '' LINES TERMINATED BY '\\n' "
                    "IGNORE 1 LINES;"
                )
            for index in table.indexes:
                index_name = _ident(f"ix_{table.name}_{'_'.join(index)}", engine)
                cols = ", ".join(_ident(c, engine) for c in index)
                lines.append(f"CREATE INDEX {index_name} ON {name} ({cols});")
            lines.append(f"ANALYZE TABLE {name};" if engine == "mysql" else f"ANALYZE {name};")
            lines.append("")
            say(f"  {database.name}.{table.name}: {csv.stat().st_size / 1e6:,.0f} MB")
        conn.close()
        (init[engine] / f"{order:02d}-{database.name}.sql").write_text(
            "\n".join(lines), encoding="utf-8"
        )

    _write_warp_configs(databases, out)
    _write_compose(out)
    return out


def _warp_db_entry(database: Database) -> str:
    if database.engine == "postgres":
        host, port, user, password = "postgres", 5432, "postgres", "${PG_PASS}"
    else:
        host, port, user, password = "mysql", 3306, "root", "${MYSQL_PASS}"
    kind = "postgresql" if database.engine == "postgres" else "mysql"
    return (
        f"  - name: {database.name}\n"
        f"    type: {kind}\n"
        f"    host: {host}\n"
        f"    port: {port}\n"
        f"    database: {database.name}\n"
        f"    username: {user}\n"
        f"    password: {password}\n"
        "    options:\n"
        "      pool_size: 10\n"
    )


def _warp_config(databases: Sequence[Database], raw_sql: bool) -> str:
    entries = "".join(_warp_db_entry(db) for db in databases)
    return (
        "# Written by `python -m scenarios export`.\n"
        f"databases:\n{entries}\n"
        "settings:\n"
        "  auto_discover_tables: true\n"
        f"  enable_raw_query: {'true' if raw_sql else 'false'}\n"
        "  raw_query_whitelist: [SELECT, WITH]\n"
        "  pagination: {default_limit: 50, max_limit: 1000}\n"
        "  export: {enabled: true, max_rows: 0, batch_size: 50000}\n"
        "  auth: {enabled: false}\n"
        "  catalog: {auto_enrich_openapi: false, auto_cross_reference: false}\n"
        "  analysis: {sample_limit: 0, include_row_count: true}\n"
    )


def _write_warp_configs(databases: Sequence[Database], out: Path) -> None:
    (out / "warp.yaml").write_text(_warp_config(databases, raw_sql=False), encoding="utf-8")
    pushdown = [db for db in databases if db.pushdown]
    (out / "warp-sql.yaml").write_text(_warp_config(pushdown, raw_sql=True), encoding="utf-8")


def _write_compose(out: Path) -> None:
    env = out / ".env"
    if not env.exists():
        env.write_text(
            f"PG_PASS={secrets.token_hex(12)}\nMYSQL_PASS={secrets.token_hex(12)}\n",
            encoding="utf-8",
        )
    warp_src = (Path(__file__).resolve().parents[3] / "warp").as_posix()

    def warp(name: str, port: int, config: str) -> str:
        return f"""  {name}:
    build: ${{WARP_SRC:-{warp_src}}}
    ports: ["{port}:8000"]
    environment:
      APP_ENV: development
      LOG_LEVEL: WARNING
      CONFIG_PATH: /app/config/database.yaml
      PG_PASS: ${{PG_PASS}}
      MYSQL_PASS: ${{MYSQL_PASS}}
    volumes:
      - ./{config}:/app/config/database.yaml:ro
    depends_on:
      postgres: {{condition: service_healthy}}
      mysql: {{condition: service_healthy}}
"""

    compose = f"""# Written by `python -m scenarios export`. See README.md.
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: ${{PG_PASS}}
    command: ["postgres", "-c", "shared_buffers=1GB", "-c", "max_wal_size=8GB",
              "-c", "timezone=UTC"]
    ports: ["55432:5432"]
    volumes:
      - ./postgres-init:/docker-entrypoint-initdb.d:ro
      - ./data:/data:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U postgres"]  # TCP: the init server has no TCP
      interval: 10s
      retries: 60
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: ${{MYSQL_PASS}}
    command: ["--secure-file-priv=", "--default-time-zone=+00:00",
              "--innodb-buffer-pool-size=1G"]
    ports: ["53306:3306"]
    volumes:
      - ./mysql-init:/docker-entrypoint-initdb.d:ro
      - ./data:/import:ro
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uroot -p$$MYSQL_ROOT_PASSWORD --silent"]
      interval: 10s
      retries: 120
{warp("warp", 8000, "warp.yaml")}{warp("warp-sql", 8001, "warp-sql.yaml")}"""
    (out / "docker-compose.yml").write_text(compose, encoding="utf-8")
    (out / "README.md").write_text(
        re.sub(
            r"^ {8}",
            "",
            """        # Scenario stack

        Postgres 16 and MySQL 8 loaded with the synthesized data, and two Warp
        instances in front of them: `:8000` refuses raw SQL, `:8001` allows it for
        the databases the scenarios push down to.

        ```bash
        docker compose up -d --build      # the first start loads the CSVs (docker compose logs -f)
        uv run --frozen python -m scenarios run --mode warp \\
            --warp-url http://localhost:8000 --warp-sql-url http://localhost:8001 --scale <same>
        docker compose down -v            # drop the databases
        ```

        Scenarios that change source data (M04, L04) run in-process only and are
        skipped here.
        """,
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )
