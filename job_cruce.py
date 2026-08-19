"""Entrypoint del cron de cruce críticos↔survey (Railway, cada 15 min).

Corre cruce_criticos_survey con push a Firestore (re-match + actualización de
estados) bajo el mismo arnés de logging durable que los otros jobs: tee de
stdout/stderr al volumen, registro del resultado en runs_cruce.jsonl y salida
distinta de cero si falla, para que el scheduler marque la ejecución como fallida.

En Railway (que solo despliega este subproyecto) el survey se baja de
$INSPECTIONS_URL y la credencial de Firestore viene de $GOOGLE_SERVICE_ACCOUNT_JSON.

Corre a mano igual que los demás: `python job_cruce.py`.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from integracion import runlog

RUNS_FILE = "runs_cruce.jsonl"


def main() -> int:
    started_at = datetime.now(timezone.utc)
    log_dir = runlog.resolve_log_dir()
    restore = runlog.start_tee(log_dir)

    print("=" * 60)
    print(f"Corrida cruce · inicio {started_at:%Y-%m-%d %H:%M:%S} UTC")
    print(f"Logs: {log_dir or 'solo stdout (sin volumen escribible)'}")
    try:
        # cruce_criticos_survey.main() lee argv: --firebase => genera JSON + sube.
        sys.argv = [sys.argv[0], "--firebase"]
        import cruce_criticos_survey
        cruce_criticos_survey.main()
        duracion = round((datetime.now(timezone.utc) - started_at).total_seconds(), 1)
        runlog.append_run(log_dir, {"estado": "ok", "duracion_seg": duracion, "archivo": RUNS_FILE})
        print("Corrida OK")
        return 0
    except Exception as exc:
        traceback.print_exc()
        runlog.append_run(log_dir, {
            "estado": "error", "archivo": RUNS_FILE,
            "duracion_seg": round((datetime.now(timezone.utc) - started_at).total_seconds(), 1),
            "error": f"{type(exc).__name__}: {exc}"})
        print("Corrida FALLIDA")
        return 1
    finally:
        restore()


if __name__ == "__main__":
    sys.exit(main())
