"""The demo must run end-to-end under the default external-access latch."""

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pandas")


def test_demo_runs_under_external_access_latch():
    from demo.demo import run_demo

    facts = run_demo(scale=0.01, quiet=True)
    assert facts["tables"] == 6
    assert facts["loaded_after_first_query"] == ["orders", "users"]
    assert facts["federation_rows"] == 3
    assert facts["view_rows"] == 3
    assert facts["cache_hit"] is True
