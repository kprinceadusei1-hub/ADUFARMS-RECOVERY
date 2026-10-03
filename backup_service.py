"""Admin-only SQLite backup/restore helpers. Never exposed directly via static routes."""
from __future__ import annotations
import shutil
import sqlite3
import os
import hashlib
import json
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BACKUP_DIR = Path(os.environ.get("ADUFARMS_BACKUP_DIR", BASE_DIR / "backups"))
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_KEEP_COUNT = max(int(os.environ.get("ADUFARMS_BACKUP_KEEP_COUNT", "90")), 3)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def list_backups():
    return sorted(BACKUP_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)


def checksum(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(path: Path) -> None:
    manifest = path.with_suffix(".json")
    manifest.write_text(json.dumps({
        "backup": path.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "size": path.stat().st_size,
        "sha256": checksum(path),
    }, indent=2), encoding="utf-8")


def verify_backup(path: str | Path) -> None:
    """Verify both SQLite integrity and the optional local checksum manifest."""
    backup = Path(path)
    verify_database(backup)
    manifest = backup.with_suffix(".json")
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("sha256") != checksum(backup):
            raise ValueError(f"Backup checksum failed: {backup.name}")


def prune_backups() -> None:
    """Keep a rolling set of verified snapshots; never delete the newest three."""
    backups = list_backups()
    for old in backups[BACKUP_KEEP_COUNT:]:
        try:
            verify_backup(old)
        except Exception:
            continue
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)


def verify_database(db_path: str | Path) -> None:
    """Raise when a SQLite database is missing or fails its integrity check."""
    path = Path(db_path)
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError("Database file is unavailable or empty.")
    conn = sqlite3.connect(str(path))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"Database integrity check failed: {result}")
    finally:
        conn.close()


def create_backup(db_path: str | Path, skip_if_unchanged: bool = False) -> Path:
    """Create timestamped backup. Never overwrites existing file (timestamp is unique;
    if collision, append counter). Returns backup path.

    With skip_if_unchanged=True (used for the automatic start-up backup) nothing new is kept when the
    data is byte-identical to the newest existing backup; that existing backup is returned instead."""
    src = Path(db_path)
    verify_database(src)
    base = f"adufarms_{timestamp()}.db"
    dst = BACKUP_DIR / base
    counter = 1
    while dst.exists():
        dst = BACKUP_DIR / f"adufarms_{timestamp()}_{counter}.db"
        counter += 1
    # Use SQLite backup API for a consistent snapshot, fallback to copy.
    try:
        src_conn = sqlite3.connect(str(src))
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
    except sqlite3.OperationalError as error:
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            if dst.exists():
                dst.unlink()
            raise ValueError(
                "The database is currently in use. Close active sessions and try the backup again."
            ) from error
        if dst.exists():
            dst.unlink()
        shutil.copy2(src, dst)
    except Exception:
        if dst.exists():
            dst.unlink()
        shutil.copy2(src, dst)
    try:
        verify_database(dst)
        _write_manifest(dst)
        verify_backup(dst)
    except Exception:
        if dst.exists():
            dst.unlink()
        dst.with_suffix(".json").unlink(missing_ok=True)
        raise
    if skip_if_unchanged:
        previous = next((b for b in list_backups() if b != dst), None)
        if previous is not None:
            try:
                if checksum(previous) == checksum(dst):
                    dst.unlink(missing_ok=True)
                    dst.with_suffix(".json").unlink(missing_ok=True)
                    return previous
            except OSError:
                pass  # cannot compare: keep the new backup, which is always safe
    prune_backups()
    return dst


def dedupe_backups(dry_run: bool = False) -> list[str]:
    """Remove backups whose content is byte-identical to an older backup (keeps the oldest of each group).

    Only automatic/manual snapshots named adufarms_*.db are considered; safety snapshots with other names
    (pre_clear_*, pre_fresh_start_*, ...) are never touched. Returns the names that were (or would be) removed.
    """
    groups: dict[str, list[Path]] = {}
    for path in sorted(BACKUP_DIR.glob("adufarms_*.db"), key=lambda p: p.stat().st_mtime):
        groups.setdefault(checksum(path), []).append(path)
    removed: list[str] = []
    for paths in groups.values():
        keep, extras = paths[0], paths[1:]
        if not extras:
            continue
        try:
            verify_database(keep)
        except Exception:
            continue  # never delete copies unless the one we keep is healthy
        for extra in extras:
            removed.append(extra.name)
            if not dry_run:
                extra.unlink(missing_ok=True)
                extra.with_suffix(".json").unlink(missing_ok=True)
    return removed


def safe_restore(db_path: str | Path, backup_name: str) -> Path:
    """Restore from a backup inside BACKUP_DIR only (prevents path traversal).
    Creates a pre-restore safety backup first. Returns pre-restore backup path."""
    safe = Path(backup_name).name
    src = BACKUP_DIR / safe
    if not src.exists() or src.suffix != ".db":
        raise ValueError("Selected backup is unavailable.")
    if src.resolve().parent != BACKUP_DIR.resolve():
        raise ValueError("Invalid backup selection.")
    verify_backup(src)
    dst = Path(db_path)
    # Safety backup before restore (never silently overwrite the only backup)
    try:
        pre = create_backup(dst)
    except sqlite3.Error as error:
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            raise ValueError(
                "The database is currently in use. Close active sessions and try the restore again."
            ) from error
        raise
    temporary = dst.with_suffix(dst.suffix + ".restore.tmp")
    try:
        source_conn = sqlite3.connect(str(src), timeout=30)
        temporary_conn = sqlite3.connect(str(temporary), timeout=30)
        try:
            source_conn.backup(temporary_conn)
        finally:
            temporary_conn.close()
            source_conn.close()
        verify_database(temporary)
        os.replace(temporary, dst)
    except PermissionError as error:
        raise ValueError(
            "The database is currently in use. Close active sessions and try the restore again."
        ) from error
    except sqlite3.OperationalError as error:
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            raise ValueError(
                "The database is currently in use. Close active sessions and try the restore again."
            ) from error
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
    verify_database(dst)
    return pre