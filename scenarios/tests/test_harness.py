"""The scenario harness itself: run with ``uv run --frozen pytest scenarios/tests``."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from scenarios import harness
from scenarios.harness import compare_rows
from scenarios.synth import ALL_DATABASES, generate, open_world


def test_compare_ignores_order_unless_asked() -> None:
    assert compare_rows([(2, "b"), (1, "a")], [(1, "a"), (2, "b")]) == ""
    assert "row 0" in compare_rows([(2, "b"), (1, "a")], [(1, "a"), (2, "b")], ordered=True)


def test_compare_numbers_by_tolerance_and_exact_when_zero() -> None:
    assert compare_rows([(1.0000000001,)], [(1.0,)], tolerance=1e-6) == ""
    assert compare_rows([(Decimal("1.50"),)], [(Decimal("1.5"),)], tolerance=0.0) == ""
    assert compare_rows([(Decimal("1.505"),)], [(Decimal("1.50"),)], tolerance=0.0) != ""
    assert compare_rows([(7,)], [(8,)], tolerance=0.5) != ""  # integers are never approximate


def test_compare_reads_zoned_timestamps_as_utc() -> None:
    zoned = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
    assert compare_rows([(zoned,)], [(datetime(2026, 10, 1, 3, 0),)]) == ""


def test_compare_date_against_midnight_only() -> None:
    assert compare_rows([(date(2026, 10, 1),)], [(datetime(2026, 10, 1),)]) == ""
    assert compare_rows([(date(2026, 10, 1),)], [(datetime(2026, 10, 1, 5),)]) != ""


def test_compare_nulls() -> None:
    assert compare_rows([(None,)], [(None,)]) == ""
    assert compare_rows([(None,)], [(0,)]) != ""


@pytest.fixture(scope="module")
def tiny(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("data")
    generate(ALL_DATABASES, 0.001, root)
    return root


def test_generation_is_deterministic(tiny: Path, tmp_path: Path) -> None:
    generate(ALL_DATABASES, 0.001, tmp_path)
    first = open_world(ALL_DATABASES, 0.001, tiny, names=["bank_core"])
    second = open_world(ALL_DATABASES, 0.001, tmp_path, names=["bank_core"])
    try:
        query = "SELECT sum(hash(t)) FROM bank_core.transactions t"
        assert first.cursor().execute(query).fetchall() == second.cursor().execute(query).fetchall()
    finally:
        first.close()
        second.close()


def test_every_table_has_its_planned_size(tiny: Path) -> None:
    world = open_world(ALL_DATABASES, 0.001, tiny)
    try:
        for name, rows in world.sizes.items():
            db, table = name.split(".")
            count = world.cursor().execute(f'SELECT count(*) FROM {db}."{table}"').fetchone()
            assert count is not None and count[0] == rows, name
    finally:
        world.close()


def test_every_scenario_runs_without_crashing(tiny: Path) -> None:
    """Steps may fail (that is what the scenarios are for); a scenario may not crash."""
    results = harness.run(
        harness.select(), harness.RunConfig(scale=0.001, root=tiny), say=lambda _m: None
    )
    crashed = {r.scenario.id: r.error for r in results if r.error}
    assert not crashed
