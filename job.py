"""Entrypoint for the scheduled run (Railway cron).

Differs from run_integration.py, which is the interactive CLI: this one takes no
arguments, always reads fresh data, never writes an Excel file, mirrors its
output to the mounted volume, records the outcome in runs.jsonl, and exits
non-zero on failure so the scheduler marks the execution as failed.

The container must exit promptly — Railway skips a scheduled execution while a
previous one is still running.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from integracion import metrics, runlog
from integracion.export_sheets import push_to_sheets, run_info
from integracion.gauth import service_account_email
from integracion.pipeline import run


def _print_history(log_dir, limit: int = 8) -> None:
    """Echo the recent run history into this execution's log.

    A cron container only exists while it runs, so there is no shell to read
    the volume from between executions. Printing the tail here is what makes
    runs.jsonl visible from the platform log viewer.
    """
    history = runlog.read_runs(log_dir, limit=limit)
    if not history:
        return
    print(f"\nÚltimas {len(history)} corridas:")
    for row in history:
        estado = row.get("estado", "?")
        detail = (f"{row.get('registros_publicados', '?')} registros · "
                  f"{row.get('tasa_match_pct', '?')}% match"
                  if estado == "ok" else row.get("error", ""))
        print(f"  {row.get('timestamp_utc', '?')}  {estado:5s}  "
              f"{row.get('duracion_seg', '?')}s  {detail}")
    print()


def main() -> int:
    started_at = datetime.now(timezone.utc)
    log_dir = runlog.resolve_log_dir()
    restore = runlog.start_tee(log_dir)

    print("=" * 60)
    print(f"Corrida programada · inicio {started_at:%Y-%m-%d %H:%M:%S} UTC")
    print(f"Logs: {log_dir or 'solo stdout (sin volumen escribible)'}")
    try:
        print(f"Service account: {service_account_email()}")
    except Exception as exc:                     # credential problems surface below
        print(f"Service account: no se pudo leer ({exc})")

    _print_history(log_dir)

    try:
        artifacts = run(use_cache=False, with_embedding=True, with_bridge=True,
                        export=False)
        metrics.print_report(artifacts["report"])

        meta = run_info(started_at, with_embedding=True,
                        bridge_info=artifacts.get("bridge_info"))
        summary = push_to_sheets(artifacts["df_consolidada"],
                                 artifacts["match_table"],
                                 artifacts["report"],
                                 run_meta=meta)

        matching = artifacts["report"]["matching"]
        print(f"\nPublicado: tabla_integrada "
              f"{summary['tabla_integrada']['filas']} filas · integracion_stats "
              f"{summary['integracion_stats']['filas']} filas")

        runlog.append_run(log_dir, {
            "estado": "ok",
            "duracion_seg": meta["duracion_seg"],
            "registros_publicados": summary["tabla_integrada"]["filas"],
            "visitas_total": matching["visitas_total"],
            "visitas_emparejadas": matching["visitas_emparejadas"],
            "tasa_match_pct": matching["tasa_match_pct"],
            "sitios_edan_total": matching["sitios_edan_total"],
            "bridge_modo": meta["bridge_modo"],
        })
        print("Corrida OK")
        return 0

    except Exception as exc:
        traceback.print_exc()
        runlog.append_run(log_dir, {
            "estado": "error",
            "duracion_seg": round(
                (datetime.now(timezone.utc) - started_at).total_seconds(), 1),
            "error": f"{type(exc).__name__}: {exc}",
        })
        print("Corrida FALLIDA")
        return 1

    finally:
        restore()


if __name__ == "__main__":
    sys.exit(main())
