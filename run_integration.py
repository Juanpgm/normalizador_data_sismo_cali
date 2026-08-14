"""
CLI entrypoint for the EDAN ↔ Visitas integration.

Examples:
  python run_integration.py                 # full run, uses cache if present
  python run_integration.py --fresh         # ignore cache, re-read Google Sheets
  python run_integration.py --no-embedding  # skip the LM tier (faster)
  python run_integration.py --no-exif       # skip the evidence-photo GPS harvest

  # what the hourly job runs: fresh data, no Excel, published to Google Sheets
  python run_integration.py --fresh --no-export --to-sheets
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from integracion import metrics
from integracion.pipeline import run

# The report is written in Spanish and uses arrows and accents. A default
# Windows console is cp1252 and raises UnicodeEncodeError on them, which would
# abort the run *after* the pipeline finished but *before* publishing.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(description="Integración EDAN ↔ Visitas (sismo Cali)")
    ap.add_argument("--fresh", action="store_true", help="ignore cache, re-read sheets")
    ap.add_argument("--no-embedding", action="store_true", help="skip the LM tier")
    ap.add_argument("--no-bridge", action="store_true", help="skip the spatial bridge")
    ap.add_argument("--no-export", action="store_true", help="do not write the Excel")
    ap.add_argument("--to-sheets", action="store_true",
                    help="publish the integrated table and stats to Google Sheets")
    ap.add_argument("--fetch-parcels", action="store_true",
                    help="try to download a Cali parcel layer first (enables parcel mode)")
    ap.add_argument("--no-exif", action="store_true",
                    help="skip harvesting GPS from the evidence photos (Drive)")
    ap.add_argument("--no-geocode", action="store_true",
                    help="skip geocoding addresses that lack coordinates")
    ap.add_argument("--geocode-limit", type=int, default=None, metavar="N",
                    help="cap Geocoding API calls this run (smoke test before full spend)")
    ap.add_argument("--no-geo-key", action="store_true",
                    help="skip the last-resort coordinate-only tier")
    args = ap.parse_args()
    started_at = datetime.now(timezone.utc)

    if args.fetch_parcels:
        from integracion.catastro_fetch import fetch_parcels
        fetch_parcels()

    artifacts = run(
        use_cache=not args.fresh,
        with_embedding=not args.no_embedding,
        with_bridge=not args.no_bridge,
        with_exif=not args.no_exif,
        with_geocode=not args.no_geocode,
        geocode_limit=args.geocode_limit,
        with_geo_key=not args.no_geo_key,
        export=not args.no_export,
    )
    metrics.print_report(artifacts["report"])
    if "excel_path" in artifacts:
        print(f"\nEntregable: {artifacts['excel_path']}")

    if args.to_sheets:
        from integracion.export_sheets import push_to_sheets, run_info
        meta = run_info(started_at,
                        with_embedding=not args.no_embedding,
                        bridge_info=artifacts.get("bridge_info"))
        summary = push_to_sheets(artifacts["df_consolidada"],
                                 artifacts["match_table"],
                                 artifacts["report"],
                                 run_meta=meta)
        print(f"\nPublicado en Google Sheets ({summary['spreadsheet_id']}):")
        for sheet in ("tabla_integrada", "integracion_stats"):
            print(f"  {sheet:20s} {summary[sheet]['filas']} filas")


if __name__ == "__main__":
    main()
