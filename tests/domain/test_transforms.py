"""Tests for the transform registry."""

import pytest

from fusion.domain.errors import QueryError
from fusion.domain.transforms import TRANSFORM_NAMES, TRANSFORMS, transform_spec


def test_registry_is_closed_and_self_consistent():
    # The registry is the whitelist: a transform an LLM invents cannot be looked
    # up, so it can never reach SQL.
    assert set(TRANSFORM_NAMES) == set(TRANSFORMS)
    assert TRANSFORM_NAMES == tuple(sorted(TRANSFORM_NAMES))
    for name, spec in TRANSFORMS.items():
        assert spec.name == name
        assert spec.description.endswith(".")
        assert spec.needs in ("order", "sequence", "calendar")


def test_every_documented_transform_is_registered():
    assert set(TRANSFORM_NAMES) == {
        "change",
        "change_pct",
        "cumsum",
        "dense_rank",
        "lag",
        "lead",
        "ntile",
        "percent_rank",
        "rank",
        "time_shift",
    }


def test_only_time_shift_needs_a_calendar():
    calendar = {name for name, spec in TRANSFORMS.items() if spec.needs == "calendar"}
    assert calendar == {"time_shift"}


def test_lookup_suggests_the_nearest_name():
    with pytest.raises(QueryError) as excinfo:
        transform_spec("cumsun", position=4)
    message = str(excinfo.value)
    assert "position 4" in message
    assert "Did you mean 'cumsum'?" in message
    # The full list, so a second guess is never needed.
    assert "percent_rank" in message


def test_lookup_of_something_unlike_anything_still_lists_everything():
    with pytest.raises(QueryError) as excinfo:
        transform_spec("zzzz")
    assert "Did you mean" not in str(excinfo.value)
    assert "Available transforms:" in str(excinfo.value)


class TestBinding:
    def test_defaults_are_filled_in(self):
        assert TRANSFORMS["lag"].bind(()) == {"n": 1}
        assert TRANSFORMS["time_shift"].bind(()) == {"periods": 1}
        assert TRANSFORMS["cumsum"].bind(()) == {}

    def test_supplied_values_win(self):
        assert TRANSFORMS["lag"].bind((("n", 3),)) == {"n": 3}

    def test_a_required_argument_is_named_with_an_example(self):
        with pytest.raises(QueryError, match=r"ntile needs a 'buckets' argument"):
            TRANSFORMS["ntile"].bind(())

    def test_an_unknown_argument_lists_the_accepted_ones(self):
        with pytest.raises(QueryError, match="Accepted arguments: n"):
            TRANSFORMS["lag"].bind((("k", 2),))

    def test_a_transform_without_arguments_says_none(self):
        with pytest.raises(QueryError, match="Accepted arguments: none"):
            TRANSFORMS["cumsum"].bind((("n", 2),))

    @pytest.mark.parametrize("value", ["2", 2.5, None, True])
    def test_non_integer_offsets_are_refused(self, value):
        # True is an int subclass, so it needs rejecting explicitly:
        # lag(x, n=True) is never what anyone meant.
        with pytest.raises(QueryError, match="whole number"):
            TRANSFORMS["lag"].bind((("n", value),))

    @pytest.mark.parametrize("value", [0, -1])
    def test_offsets_below_the_minimum_are_refused(self, value):
        with pytest.raises(QueryError, match="at least 1"):
            TRANSFORMS["lag"].bind((("n", value),))


def test_as_dict_describes_the_arguments():
    described = TRANSFORMS["ntile"].as_dict()
    assert described["name"] == "ntile"
    assert described["needs"] == "order"
    assert described["arguments"] == [{"name": "buckets", "required": True, "default": None}]
    assert TRANSFORMS["lag"].as_dict()["arguments"] == [
        {"name": "n", "required": False, "default": 1}
    ]
