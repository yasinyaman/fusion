"""Tests for BackupService (export directories, snapshots, retention)."""

import os
import time

import pytest

from fusion.adapters.outbound.duckdb_store import DuckDBStore
from fusion.application.backup import BackupService
from fusion.domain.errors import BackupError, QueryError
from fusion.domain.models import RowSet, TableRef
from tests.fakes import ManualScheduler


def _seed(store):
    store.create_schema("s")
    store.materialize(TableRef("s", "t"), RowSet.from_records([{"a": 1}, {"a": 2}]))


@pytest.fixture
def memory_store():
    s = DuckDBStore(threads=1, memory_limit="256MB", external_access=True)
    _seed(s)
    yield s
    s.close()


@pytest.fixture
def file_store(tmp_path):
    s = DuckDBStore(database=str(tmp_path / "live.duckdb"), threads=1, memory_limit="256MB")
    _seed(s)
    yield s
    s.close()


class TestInMemory:
    def test_in_memory_backup_exports_directory_and_is_listed(self, memory_store, tmp_path):
        """Regression: in-memory databases were never backed up (path probe bug)."""
        service = BackupService(memory_store, tmp_path / "b", enabled=True)
        info = service.create_backup()
        assert info.kind == "export"
        assert info.path.is_dir()
        assert info.name.startswith("fusion_backup_")
        assert info.size_bytes > 0
        listed = service.list_backups()
        assert [b.name for b in listed] == [info.name]
        assert service.get_stats()["total_backups"] == 1

    def test_in_memory_backup_without_external_access_raises_backup_error(self, tmp_path):
        store = DuckDBStore(threads=1, memory_limit="256MB")  # latch on
        try:
            service = BackupService(store, tmp_path / "b", enabled=True)
            with pytest.raises(BackupError, match="external access"):
                service.create_backup()
        finally:
            store.close()

    def test_restore_export_into_fresh_store(self, memory_store, tmp_path):
        service = BackupService(memory_store, tmp_path / "b", enabled=True)
        info = service.create_backup()
        fresh = DuckDBStore(threads=1, memory_limit="256MB", external_access=True)
        try:
            BackupService(fresh, tmp_path / "b", enabled=True).restore_backup(info.path)
            assert fresh.count("s.t") == 2
        finally:
            fresh.close()


class TestFileBased:
    def test_snapshot_file(self, file_store, tmp_path):
        service = BackupService(file_store, tmp_path / "b", enabled=True)
        info = service.create_backup()
        assert info.kind == "file"
        assert info.path.suffix == ".duckdb"
        assert info.path.is_file()

    def test_restore_keeps_latch(self, file_store, tmp_path):
        """Regression: restoring reopened DuckDB without the security latch."""
        service = BackupService(file_store, tmp_path / "b", enabled=True)
        info = service.create_backup()
        file_store.materialize(TableRef("s", "t"), RowSet.from_records([{"a": 9}]))
        service.restore_backup(info.path)
        assert file_store.count("s.t") == 2
        latch = file_store.execute("SELECT current_setting('enable_external_access')").rows[0][0]
        assert latch is False
        with pytest.raises(QueryError):
            file_store.execute("SELECT * FROM read_text('/etc/hosts')")

    def test_restore_missing(self, file_store, tmp_path):
        with pytest.raises(BackupError):
            BackupService(file_store, tmp_path, enabled=True).restore_backup(tmp_path / "nope")


class TestPolicy:
    def test_disabled_raises(self, memory_store, tmp_path):
        service = BackupService(memory_store, tmp_path / "b", enabled=False)
        with pytest.raises(BackupError, match="disabled"):
            service.create_backup()
        assert service.list_backups() == []

    def test_cleanup_old_backups(self, memory_store, tmp_path):
        now = time.time()
        service = BackupService(
            memory_store, tmp_path / "b", retention_days=7, enabled=True, clock=lambda: now
        )
        old = service.create_backup()
        fresh_dir = tmp_path / "b" / "fusion_backup_99999999_000000"
        fresh_dir.mkdir()
        stale = now - 8 * 86400
        os.utime(old.path, (stale, stale))
        assert service.cleanup_old_backups() == 1
        assert [b.name for b in service.list_backups()] == [fresh_dir.name]

    def test_scheduler_start_stop_and_run(self, memory_store, tmp_path):
        scheduler = ManualScheduler()
        service = BackupService(
            memory_store, tmp_path / "b", interval=60, enabled=True, scheduler=scheduler
        )
        service.start()
        assert service.running
        service.start()  # idempotent
        assert len(scheduler.jobs) == 1
        scheduler.tick()
        assert service.get_stats()["total_backups"] == 1
        service.stop()
        assert not service.running
        assert scheduler.jobs[0].cancelled

    def test_start_when_disabled_is_noop(self, memory_store, tmp_path):
        service = BackupService(
            memory_store, tmp_path / "b", enabled=False, scheduler=ManualScheduler()
        )
        service.start()
        assert not service.running

    def test_scheduled_failure_is_logged_not_raised(self, tmp_path):
        store = DuckDBStore(threads=1, memory_limit="256MB")  # export will fail (latch)
        try:
            scheduler = ManualScheduler()
            service = BackupService(store, tmp_path / "b", enabled=True, scheduler=scheduler)
            service.start()
            scheduler.tick()  # must not raise
            assert service.list_backups() == []
        finally:
            store.close()

    def test_stats_shape(self, memory_store, tmp_path):
        stats = BackupService(
            memory_store, tmp_path / "b", interval=5, retention_days=2
        ).get_stats()
        assert stats == {
            "enabled": False,
            "backup_path": str(tmp_path / "b"),
            "interval_seconds": 5,
            "retention_days": 2,
            "total_backups": 0,
            "total_size_mb": 0.0,
            "running": False,
        }
