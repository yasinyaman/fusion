"""The synthetic source databases, by sector."""

from __future__ import annotations

from scenarios.synth import banking, healthcare, logistics, manufacturing, retail, telecom
from scenarios.synth.core import Database, Table, World, generate, open_world

ALL_DATABASES: tuple[Database, ...] = (
    *banking.DATABASES,
    *retail.DATABASES,
    *telecom.DATABASES,
    *healthcare.DATABASES,
    *manufacturing.DATABASES,
    *logistics.DATABASES,
)

SECTORS: tuple[str, ...] = tuple(dict.fromkeys(db.sector for db in ALL_DATABASES))


def database(name: str) -> Database:
    for db in ALL_DATABASES:
        if db.name == name:
            return db
    raise KeyError(name)


__all__ = [
    "ALL_DATABASES",
    "SECTORS",
    "Database",
    "Table",
    "World",
    "database",
    "generate",
    "open_world",
]
