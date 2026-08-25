"""Entrypoint for the scheduled cruce_sticker run (Railway cron — manual
service creation pending, see design.md ADR-2 correction / tasks.md 1.7).

Wraps cruce_sticker.main() with the same durable-logging harness as
job_asignaciones.py: tees stdout/stderr to the mounted volume, records the
outcome in runs_sticker.jsonl, and exits non-zero on failure so the scheduler
marks the execution as failed.

Runnable by hand exactly like the others: `python job_sticker.py`.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cruce_sticker
from integracion import runlog

RUNS_FILE = "runs_sticker.jsonl"


def main() -> int:
    started_at = datetime.now(timezone.utc)
    log_dir = runlog.resolve_log_dir()
    restore = runlog.start_tee(log_dir)

    print("=" * 60)
    print(f"Corrida cruce_sticker · inicio {started_at:%Y-%m-%d %H:%M:%S} UTC")
    print(f"Logs: {log_dir or 'solo stdout (sin volumen escribible)'}")
    try:
        summary = cruce_sticker.main() or {}
        duracion = round((datetime.now(timezone.utc) - started_at).total_seconds(), 1)
        runlog.append_run(log_dir, {"estado": "ok", "duracion_seg": duracion,
                                    "archivo": RUNS_FILE, **summary})
        print("Corrida OK")
        return 0
    except Exception as exc:
        traceback.print_exc()
        runlog.append_run(log_dir, {
            "estado": "error", "archivo": RUNS_FILE,
            "duracion_seg": round(
                (datetime.now(timezone.utc) - started_at).total_seconds(), 1),
            "error": f"{type(exc).__name__}: {exc}"})
        print("Corrida FALLIDA")
        return 1
    finally:
        restore()


if __name__ == "__main__":
    sys.exit(main())
