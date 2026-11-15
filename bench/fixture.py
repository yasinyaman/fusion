"""The benchmark's dataset, in one place.

Small and fixed on purpose: whether a query is *right* shows as well on
fifteen rows as on a million, and a deterministic dataset is what makes the
gold answers reproducible. The same rows are loaded into whichever engine is
under test, so the three arms are graded against identical data.
"""

from __future__ import annotations

from typing import Any

MUSTERILER: list[tuple[Any, ...]] = [
    (1, "Ayse Yilmaz", "ayse@example.com", "IST01", "premium", "2022-01-15"),
    (2, "Mehmet Demir", "mehmet@example.com", "IST01", "standart", "2022-03-02"),
    (3, "Zeynep Kaya", None, "ANK02", "premium", "2023-05-20"),
    (4, "Ali Sahin", "ali@example.com", "ANK02", "temel", "2023-07-11"),
    (5, "Fatma Celik", None, "IZM03", "standart", "2024-02-01"),
]

ISLEMLER: list[tuple[Any, ...]] = [
    (1, 1, 1500.00, "TRY", "havale", "basarili", "2024-01-10", "mobil"),
    (2, 1, 250.50, "TRY", "odeme", "basarili", "2024-01-25", "web"),
    (3, 2, 900.00, "TRY", "havale", "iptal", "2024-02-05", "mobil"),
    (4, 2, 3200.75, "USD", "transfer", "basarili", "2024-02-18", "sube"),
    (5, 3, 120.00, "TRY", "odeme", "basarili", "2024-04-03", "mobil"),
    (6, 3, 4500.00, "TRY", "transfer", "basarili", "2024-04-22", "web"),
    (7, 4, 75.25, "EUR", "odeme", "iptal", "2024-05-09", "mobil"),
    (8, 4, 2100.00, "TRY", "havale", "basarili", "2024-05-30", "sube"),
    (9, 5, 640.00, "TRY", "odeme", "basarili", "2024-06-14", "web"),
    (10, 5, 1800.00, "TRY", "transfer", "basarili", "2024-06-28", "mobil"),
]

DUCKDB_DDL = """
CREATE TABLE musteriler (
  musteri_id    INTEGER PRIMARY KEY,
  ad_soyad      VARCHAR NOT NULL,
  eposta        VARCHAR,
  sube_kodu     VARCHAR NOT NULL,
  segment       VARCHAR NOT NULL,
  acilis_tarihi DATE NOT NULL
);
CREATE TABLE islemler (
  islem_id     INTEGER PRIMARY KEY,
  musteri_id   INTEGER NOT NULL,
  tutar        DOUBLE NOT NULL,
  para_birimi  VARCHAR NOT NULL,
  islem_turu   VARCHAR NOT NULL,
  durum        VARCHAR NOT NULL,
  islem_tarihi DATE NOT NULL,
  kanal        VARCHAR NOT NULL
);
"""


def load_duckdb(connection: Any) -> Any:
    """Create the tables and insert the rows."""
    for statement in DUCKDB_DDL.strip().split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.executemany("INSERT INTO musteriler VALUES (?, ?, ?, ?, ?, ?)", MUSTERILER)
    connection.executemany("INSERT INTO islemler VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ISLEMLER)
    return connection


def as_records(rows: list[tuple[Any, ...]], columns: list[str]) -> list[dict[str, Any]]:
    """Rows as dicts, for a source that serves them to Fusion."""
    return [dict(zip(columns, row, strict=True)) for row in rows]


MUSTERILER_COLUMNS = [
    "musteri_id",
    "ad_soyad",
    "eposta",
    "sube_kodu",
    "segment",
    "acilis_tarihi",
]
ISLEMLER_COLUMNS = [
    "islem_id",
    "musteri_id",
    "tutar",
    "para_birimi",
    "islem_turu",
    "durum",
    "islem_tarihi",
    "kanal",
]
