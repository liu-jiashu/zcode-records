from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
SOURCES = {
    "conversation": HOME / ".zcode" / "cli" / "db" / "db.sqlite",
    "tasks_index": HOME / ".zcode" / "v2" / "tasks-index.sqlite",
}
OUT_DIR = Path(r"D:\zcode-records-work")
SNAPSHOTS = {
    "conversation": OUT_DIR / "source-snapshot-db.sqlite",
    "tasks_index": OUT_DIR / "source-snapshot-tasks-index.sqlite",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    meta = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "sources": {},
        "snapshots": {},
        "method": (
            "sqlite3.Connection.backup() from a source opened with mode=ro&immutable=0; "
            "PRAGMA query_only=ON on the source; destination is a separate standalone file"
        ),
    }
    for key, src_path in SOURCES.items():
        if not src_path.is_file():
            raise FileNotFoundError(src_path)
        out_path = SNAPSHOTS[key]
        if out_path.exists():
            raise FileExistsError(f"refusing to overwrite {out_path}")
        before = src_path.stat()
        uri = "file:" + src_path.as_posix() + "?mode=ro"
        src = sqlite3.connect(uri, uri=True, timeout=60)
        try:
            src.execute("PRAGMA query_only=ON")
            src.execute("PRAGMA busy_timeout=60000")
            dst = sqlite3.connect(out_path)
            try:
                src.backup(dst, pages=1024, sleep=0.05)
                dst.execute("PRAGMA journal_mode=DELETE")
                dst.commit()
            finally:
                dst.close()
            integrity = src.execute("PRAGMA quick_check").fetchone()
        finally:
            src.close()
        after = src_path.stat()
        snapshot_check = sqlite3.connect(out_path)
        try:
            snap_integrity = snapshot_check.execute("PRAGMA quick_check").fetchone()
            snap_sessions = None
            if key == "conversation":
                snap_sessions = snapshot_check.execute("SELECT count(*) FROM session").fetchone()[0]
        finally:
            snapshot_check.close()
        meta["sources"][key] = {
            "path": str(src_path),
            "size_bytes_before": before.st_size,
            "size_bytes_after": after.st_size,
            "mtime_before_utc": datetime.fromtimestamp(before.st_mtime, timezone.utc).isoformat(),
            "mtime_after_utc": datetime.fromtimestamp(after.st_mtime, timezone.utc).isoformat(),
            "sha256_before": sha256_file(src_path),
            "quick_check": integrity[0] if integrity else None,
            "opened_read_only": True,
        }
        meta["snapshots"][key] = {
            "path": str(out_path),
            "size_bytes": out_path.stat().st_size,
            "sha256": sha256_file(out_path),
            "quick_check": snap_integrity[0] if snap_integrity else None,
            "session_count": snap_sessions,
        }
        print(f"{key}: {src_path} -> {out_path} ({out_path.stat().st_size} bytes)")
    (OUT_DIR / "snapshot-meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(meta["snapshots"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
