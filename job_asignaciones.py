"""Entrypoint for the scheduled asignaciones run (Railway cron, daily 16:00 Bogota).

Wraps asignar_f3.main() with the same durable-logging harness as job.py: tees
stdout/stderr to the mounted volume, records the outcome in runs_asignaciones.jsonl,
and exits non-zero on failure so the scheduler marks the execution as failed.

Runnable by hand exactly like the others: `python job_asignaciones.py`.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asignar_f3
from integracion import runlog

RUNS_FILE = "runs_asignaciones.jsonl"


def main() -> int:
    started_at = datetime.now(timezone.utc)
    log_dir = runlog.resolve_log_dir()
    restore = runlog.start_tee(log_dir)

    print("=" * 60)
    print(f"Corrida asignaciones · inicio {started_at:%Y-%m-%d %H:%M:%S} UTC")
    print(f"Logs: {log_dir or 'solo stdout (sin volumen escribible)'}")
    try:
        summary = asignar_f3.main() or {}
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
