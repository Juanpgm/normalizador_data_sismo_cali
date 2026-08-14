"""Durable logging for the scheduled job.

A Railway cron service is an ephemeral container: stdout goes to the platform's
log viewer, which has a retention window and no structure. This module keeps a
copy that outlives the container:

* ``integracion.log`` — everything the run printed, rotated, on the mounted
  volume;
* ``runs.jsonl`` — one line per execution with the outcome and headline
  numbers, so the history is queryable without reading prose.

The pipeline prints rather than logs, so instead of rewriting every call site
stdout and stderr are teed: the platform still sees them live, and the volume
keeps the copy.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BOGOTA = timezone(timedelta(hours=-5))
DEFAULT_LOG_DIR = "/data/logs"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
RUNS_FILE = "runs.jsonl"
LOG_FILE = "integracion.log"


def resolve_log_dir(preferred: str | None = None) -> Path | None:
    """Pick a writable log directory, or None when there is nowhere to write.

    Order: the explicit argument, ``LOG_DIR``, then the volume mount point.
    Logging is a convenience, never a reason to fail a run — if none of these
    is writable we return None and the caller carries on with stdout only.
    """
    for candidate in (preferred, os.environ.get("LOG_DIR"), DEFAULT_LOG_DIR):
        if not candidate:
            continue
        path = Path(candidate)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return path
        except (OSError, ValueError):
            # ValueError covers malformed paths (e.g. a LOG_DIR with a null
            # byte), which pathlib raises before it ever touches the disk.
            continue
    return None


class _Tee:
    """Write-through stream: forwards to the console and to the log file."""

    def __init__(self, primary, handler):
        self._primary, self._handler = primary, handler

    def write(self, text):
        self._primary.write(text)
        if text.strip():
            self._handler.emit_text(text.rstrip("\n"))
        return len(text)

    def flush(self):
        self._primary.flush()

    def isatty(self):
        return getattr(self._primary, "isatty", lambda: False)()


class _LineWriter:
    """Append-only file sink that timestamps each line and rotates by size."""

    def __init__(self, path: Path, max_bytes: int = MAX_BYTES,
                 backup_count: int = BACKUP_COUNT):
        self._path = Path(path)
        self._max_bytes, self._backups = max_bytes, backup_count
        self._fh = open(self._path, "a", encoding="utf-8")

    def emit_text(self, text):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for line in text.splitlines():
            self._fh.write(f"{stamp}  {line}\n")
        self._fh.flush()
        if self._fh.tell() > self._max_bytes:
            self._rotate()

    def _rotate(self):
        self._fh.close()
        for i in range(self._backups - 1, 0, -1):
            older = self._path.with_suffix(self._path.suffix + f".{i}")
            if older.exists():
                older.replace(self._path.with_suffix(self._path.suffix + f".{i + 1}"))
        self._path.replace(self._path.with_suffix(self._path.suffix + ".1"))
        self._fh = open(self._path, "a", encoding="utf-8")

    def close(self):
        self._fh.close()


def start_tee(log_dir: Path | None):
    """Mirror stdout/stderr into the rotating log file. Returns a restore fn."""
    if log_dir is None:
        return lambda: None

    writer = _LineWriter(log_dir / LOG_FILE)
    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(original_out, writer)
    sys.stderr = _Tee(original_err, writer)

    def restore():
        sys.stdout, sys.stderr = original_out, original_err
        writer.close()

    return restore


def append_run(log_dir: Path | None, record: dict) -> None:
    """Append one execution record to runs.jsonl. Never raises."""
    if log_dir is None:
        return
    now = datetime.now(timezone.utc)
    row = {"timestamp_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
           "timestamp_bogota": now.astimezone(BOGOTA).strftime("%Y-%m-%d %H:%M:%S"),
           **record}
    try:
        with open(log_dir / RUNS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:                       # pragma: no cover - disk issues
        print(f"[runlog] no se pudo escribir runs.jsonl: {exc}")


def read_runs(log_dir: Path | None, limit: int = 20) -> list[dict]:
    """Last ``limit`` execution records, oldest first. Used to inspect history."""
    if log_dir is None:
        return []
    path = Path(log_dir) / RUNS_FILE
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
