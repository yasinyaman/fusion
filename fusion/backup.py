"""Backup and restore functionality for DuckDB state."""

import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from fusion.config import config

if TYPE_CHECKING:
    from fusion.engine import OLAPEngine

logger = logging.getLogger(__name__)


class BackupManager:
    """Manages periodic backups of DuckDB database.
    
    Creates timestamped backups and handles retention policy.
    """

    def __init__(
        self,
        engine: "OLAPEngine",
        backup_path: str = None,
        interval: int = None,
        retention_days: int = None,
        enabled: bool = None,
    ):
        self._engine = engine
        self.backup_path = Path(backup_path or config.BACKUP_PATH)
        self.interval = interval or config.BACKUP_INTERVAL
        self.retention_days = retention_days or config.BACKUP_RETENTION_DAYS
        self.enabled = enabled if enabled is not None else config.BACKUP_ENABLED
        
        self._timer: Optional[threading.Timer] = None
        self._running = False
        
        # Create backup directory
        if self.enabled:
            self.backup_path.mkdir(parents=True, exist_ok=True)
            logger.info(
                f"BackupManager initialized: path={self.backup_path}, "
                f"interval={self.interval}s, retention={self.retention_days}d"
            )
    
    def start(self) -> None:
        """Start periodic backup scheduler."""
        if not self.enabled:
            logger.info("Backup is disabled")
            return
        
        if self._running:
            logger.warning("Backup scheduler already running")
            return
        
        self._running = True
        self._schedule_next()
        logger.info("Backup scheduler started")
    
    def stop(self) -> None:
        """Stop periodic backup scheduler."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        logger.info("Backup scheduler stopped")
    
    def _schedule_next(self) -> None:
        """Schedule next backup."""
        if not self._running:
            return
        
        self._timer = threading.Timer(self.interval, self._run_backup)
        self._timer.daemon = True
        self._timer.start()
    
    def _run_backup(self) -> None:
        """Execute backup and schedule next one."""
        try:
            self.create_backup()
            self.cleanup_old_backups()
        except Exception as e:
            logger.error(f"Backup failed: {e}")
        finally:
            self._schedule_next()
    
    def create_backup(self) -> Path:
        """Create a timestamped backup of DuckDB database.
        
        Returns:
            Path to backup file
        """
        if not self.enabled:
            raise ValueError("Backup is disabled")
        
        # Generate backup filename
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backup_path / f"fusion_backup_{timestamp}.duckdb"
        
        # Get current database path
        row = self._engine._conn.execute("SELECT current_database()").fetchone()
        db_path = row[0] if row else ""

        if db_path == ":memory:":
            # For in-memory databases, export to file
            logger.info(f"Creating backup of in-memory database to {backup_file}")
            with self._engine._lock:
                self._engine._conn.execute(f"EXPORT DATABASE '{backup_file}' (FORMAT PARQUET)")
        else:
            # For file-based databases, copy the file
            logger.info(f"Creating backup of {db_path} to {backup_file}")
            shutil.copy2(db_path, backup_file)
        
        logger.info(f"Backup created: {backup_file}")
        return backup_file
    
    def restore_backup(self, backup_file: Path) -> None:
        """Restore database from backup file.
        
        Args:
            backup_file: Path to backup file
        
        Raises:
            FileNotFoundError: If backup file doesn't exist
            ValueError: If engine uses in-memory database
        """
        if not backup_file.exists():
            raise FileNotFoundError(f"Backup file not found: {backup_file}")
        
        row = self._engine._conn.execute("SELECT current_database()").fetchone()
        db_path = row[0] if row else ""

        if db_path == ":memory:":
            raise ValueError("Cannot restore to in-memory database. Use IMPORT DATABASE instead.")
        
        logger.info(f"Restoring backup from {backup_file} to {db_path}")
        
        # Close current connection
        self._engine._conn.close()
        
        # Copy backup file
        shutil.copy2(backup_file, db_path)
        
        # Reconnect
        import duckdb
        self._engine._conn = duckdb.connect(db_path)
        
        logger.info("Backup restored successfully")
    
    def list_backups(self) -> list[dict]:
        """List all available backups.
        
        Returns:
            List of backup info dicts with name, path, size, created_at
        """
        if not self.backup_path.exists():
            return []
        
        backups = []
        for backup_file in sorted(self.backup_path.glob("fusion_backup_*.duckdb")):
            stat = backup_file.stat()
            backups.append({
                "name": backup_file.name,
                "path": str(backup_file),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
            })
        
        return backups
    
    def cleanup_old_backups(self) -> int:
        """Remove backups older than retention period.
        
        Returns:
            Number of backups deleted
        """
        if not self.backup_path.exists():
            return 0
        
        cutoff_time = time.time() - (self.retention_days * 86400)
        deleted_count = 0
        
        for backup_file in self.backup_path.glob("fusion_backup_*.duckdb"):
            if backup_file.stat().st_ctime < cutoff_time:
                logger.info(f"Deleting old backup: {backup_file}")
                backup_file.unlink()
                deleted_count += 1
        
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} old backups")
        
        return deleted_count
    
    def get_stats(self) -> dict:
        """Get backup manager statistics.
        
        Returns:
            Dict with backup stats
        """
        backups = self.list_backups()
        total_size = sum(b["size_bytes"] for b in backups)
        
        return {
            "enabled": self.enabled,
            "backup_path": str(self.backup_path),
            "interval_seconds": self.interval,
            "retention_days": self.retention_days,
            "total_backups": len(backups),
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "running": self._running,
        }
