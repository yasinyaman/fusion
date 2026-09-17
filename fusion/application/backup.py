"""BackupService: timestamped backups with retention, via the store port.

In-memory databases are backed up with ``EXPORT DATABASE`` (a directory);
file-based databases are snapshotted to a ``.duckdb`` file.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fusion.domain.errors import BackupError
from fusion.domain.models import BackupInfo
from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.scheduler import ScheduledJob, Scheduler

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "fusion_backup_"


class BackupService:
    def __init__(
        self,
        store: AnalyticsStore,
        backup_dir: str | Path,
        interval: int = 3600,
        retention_days: int = 7,
        enabled: bool = False,
        scheduler: Scheduler | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self.backup_dir = Path(backup_dir)
        self.interval = interval
        self.retention_days = retention_days
        self.enabled = enabled
        self._scheduler = scheduler
        self._clock = clock
        self._job: ScheduledJob | None = None

    # -- scheduling ---------------------------------------------------------

    def start(self) -> None:
        if not self.enabled:
            logger.info("Backup is disabled")
            return
        if self._job is not None:
            logger.warning("Backup scheduler already running")
            return
        if self._scheduler is None:
            raise RuntimeError("BackupService has no scheduler")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._job = self._scheduler.every(self.interval, self._run, name="backup")
        logger.info(
            "Backup scheduler started (path=%s, every %ss, retention=%dd)",
            self.backup_dir,
            self.interval,
            self.retention_days,
        )

    def stop(self) -> None:
        if self._job is not None:
            self._job.cancel()
            self._job = None
            logger.info("Backup scheduler stopped")

    @property
    def running(self) -> bool:
        return self._job is not None

    def _run(self) -> None:
        try:
            self.create_backup()
            self.cleanup_old_backups()
        except Exception as e:
            logger.error("Scheduled backup failed: %s", e)

    # -- operations ---------------------------------------------------------

    def create_backup(self) -> BackupInfo:
        if not self.enabled:
            raise BackupError("Backup is disabled (set FUSION_BACKUP_ENABLED=true)")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

        if self._store.database_path is None:
            target = self.backup_dir / f"{BACKUP_PREFIX}{stamp}"
            logger.info("Exporting in-memory database to %s", target)
            self._store.export_to(target)
        else:
            target = self.backup_dir / f"{BACKUP_PREFIX}{stamp}.duckdb"
            logger.info("Snapshotting %s to %s", self._store.database_path, target)
            self._store.snapshot_to(target)

        info = self._describe(target)
        logger.info("Backup created: %s (%s)", info.name, info.kind)
        return info

    def restore_backup(self, path: str | Path) -> None:
        backup = Path(path)
        if not backup.exists():
            raise BackupError(f"Backup not found: {backup}")
        if backup.is_dir():
            self._store.import_from(backup)
        else:
            self._store.restore_from(backup)
        logger.info("Backup restored from %s", backup)

    def list_backups(self) -> list[BackupInfo]:
        if not self.backup_dir.exists():
            return []
        return [self._describe(p) for p in sorted(self.backup_dir.glob(f"{BACKUP_PREFIX}*"))]

    def cleanup_old_backups(self) -> int:
        if not self.backup_dir.exists():
            return 0
        cutoff = self._clock() - self.retention_days * 86400
        deleted = 0
        for path in self.backup_dir.glob(f"{BACKUP_PREFIX}*"):
            if path.stat().st_mtime < cutoff:
                logger.info("Deleting old backup: %s", path)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                deleted += 1
        if deleted:
            logger.info("Cleaned up %d old backups", deleted)
        return deleted

    def get_stats(self) -> dict[str, Any]:
        backups = self.list_backups()
        total = sum(b.size_bytes for b in backups)
        return {
            "enabled": self.enabled,
            "backup_path": str(self.backup_dir),
            "interval_seconds": self.interval,
            "retention_days": self.retention_days,
            "total_backups": len(backups),
            "total_size_mb": round(total / (1024 * 1024), 2),
            "running": self.running,
        }

    @staticmethod
    def _describe(path: Path) -> BackupInfo:
        kind: Literal["file", "export"]
        if path.is_dir():
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            kind = "export"
        else:
            size = path.stat().st_size
            kind = "file"
        return BackupInfo(
            name=path.name,
            path=path,
            kind=kind,
            size_bytes=size,
            created_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        )
