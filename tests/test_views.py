"""Tests for MaterializedViewManager."""

import pytest

from fusion.exceptions import QueryError
from fusion.views.materialized import MaterializedViewManager


class TestMaterializedViewManager:
    def test_create_view(self, engine_with_data):
        mv = MaterializedViewManager(engine_with_data)
        mv.create("test_view", "SELECT segment, COUNT(*) as cnt FROM test_db.users GROUP BY segment")
        views = mv.list_views()
        assert len(views) == 1
        assert views[0]["name"] == "test_view"
        mv.close()

    def test_get_view(self, engine_with_data):
        mv = MaterializedViewManager(engine_with_data)
        mv.create("test_view", "SELECT segment, COUNT(*) as cnt FROM test_db.users GROUP BY segment")
        result = mv.get("test_view")
        assert result.row_count > 0
        mv.close()

    def test_refresh_view(self, engine_with_data):
        mv = MaterializedViewManager(engine_with_data)
        mv.create("test_view", "SELECT COUNT(*) as cnt FROM test_db.users")
        mv.refresh("test_view")
        result = mv.get("test_view")
        assert result.to_dict()[0]["cnt"] == 5
        mv.close()

    def test_drop_view(self, engine_with_data):
        mv = MaterializedViewManager(engine_with_data)
        mv.create("test_view", "SELECT 1 AS x")
        mv.drop("test_view")
        assert len(mv.list_views()) == 0
        mv.close()

    def test_get_nonexistent_view(self, engine_with_data):
        mv = MaterializedViewManager(engine_with_data)
        with pytest.raises(QueryError):
            mv.get("nonexistent")
        mv.close()

    def test_refresh_nonexistent_view(self, engine_with_data):
        mv = MaterializedViewManager(engine_with_data)
        with pytest.raises(QueryError):
            mv.refresh("nonexistent")
        mv.close()

    def test_parse_refresh_interval(self, engine_with_data):
        mv = MaterializedViewManager(engine_with_data)
        assert mv._parse_refresh_interval("manual") == 0
        assert mv._parse_refresh_interval("hourly") == 3600
        assert mv._parse_refresh_interval("daily") == 86400
        assert mv._parse_refresh_interval("every 15 minutes") == 900
        assert mv._parse_refresh_interval("every 2 hours") == 7200
        mv.close()
