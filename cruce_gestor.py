"""Cross-reference ("cruce") the F3 assignment roster against the "Gestor de
Zonas — PMU Cali" Apps Script API: which critical points already have a field
visit (F3) done, which gestor zone each critical point falls in, and a
per-zone rollup of remaining work.

Reads web/data/asignaciones.json (the roster asignar_f3.py exports) and the
gestor's `zonas`, `evaluaciones`, `puntos` and `despachosHoy` endpoints. For
each roster record, finds the nearest gestor `evaluaciones` row (an F3 visit
already done) and the nearest gestor `puntos` row, both within 40 m
(haversine). Writes web/data/cruce_gestor.json: a global resumen, a per-zone
rollup (faltantes desc) and the enriched roster records.

    python cruce_gestor.py --check   # offline self-check, no network
    python cruce_gestor.py           # real data, write web/data/cruce_gestor.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from integracion.coords import haversine_m

BASE_URL = ("https://script.google.com/macros/s/"
            "AKfycbw4uRh_HkiZtKgvb1ZkGYt_m5wydNFWw1SkN6D2ttGEaLm_V4cHOJCqg5I581hqevuP/exec")
MAX_MATCH_M = 40.0
PENDING_STATES = {"PENDIENTE", "EN_ATENCION"}

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[0]
ASIGNACIONES_JSON = REPO_ROOT / "web" / "data" / "asignaciones.json"
OUT_JSON = REPO_ROOT / "web" / "data" / "cruce_gestor.json"


# ── Gestor API ──────────────────────────────────────────────────────────────
def fetch_gestor(api: str, tries: int = 3, sleep_s: float = 1.5) -> list[dict]:
    """GET ?api=<api>, parsed as JSON. Apps Script occasionally returns an HTML
    wrapper instead of JSON (transient); retry a few times before giving up."""
    last_err: Exception | None = None
    for attempt in range(tries):
        try:
            resp = requests.get(BASE_URL, params={"api": api}, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            if attempt < tries - 1:
                time.sleep(sleep_s)
    raise RuntimeError(f"gestor api={api} falló tras {tries} intentos: {last_err}")


# ── Geo matching ────────────────────────────────────────────────────────────
def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def nearest(lat, lon, items: list[dict], get_latlon, max_m: float = MAX_MATCH_M):
    """Closest item (by haversine) within max_m, or (None, None). `get_latlon`
    returns (lat, lon) or None for an item with unparseable coordinates."""
    if lat is None or lon is None:
        return None, None
    best, best_d = None, None
    for it in items:
        ll = get_latlon(it)
        if ll is None:
            continue
        d = haversine_m((lat, lon), ll)
        if d <= max_m and (best_d is None or d < best_d):
            best, best_d = it, d
    return best, best_d


def _eval_latlon(e: dict):
    lat, lon = _num(e.get("Y")), _num(e.get("X"))
    return (lat, lon) if lat is not None and lon is not None else None


def _punto_latlon(p: dict):
    lat, lon = _num(p.get("LAT")), _num(p.get("LON"))
    return (lat, lon) if lat is not None and lon is not None else None


def cruce_f3(lat, lon, evaluaciones: list[dict]) -> dict:
    """Nearest F3 visit already done within 40 m; f3_hecha=False when none."""
    best, dist = nearest(lat, lon, evaluaciones, _eval_latlon)
    if best is None:
        return {"f3_hecha": False, "f3_global_id": None, "f3_fecha": None,
                "f3_severidad": None, "f3_habitabilidad": None, "f3_dist_m": None}
    return {"f3_hecha": True, "f3_global_id": best.get("GLOBAL_ID"),
            "f3_fecha": best.get("FECHA_INSPECCION"), "f3_severidad": best.get("SEVERIDAD"),
            "f3_habitabilidad": best.get("HABITABILIDAD"), "f3_dist_m": round(dist, 1)}


def cruce_punto(lat, lon, puntos: list[dict]) -> dict:
    """Nearest gestor `puntos` entry within 40 m (its status)."""
    best, _ = nearest(lat, lon, puntos, _punto_latlon)
    return {"punto_estado": best.get("ESTADO") if best else None}


# ── Zone rollup ─────────────────────────────────────────────────────────────
FUERA_DE_ZONA = "(fuera de zona)"


def build_zona_rollups(zonas: list[dict], enriched: list[dict],
                       despachos_hoy: list[dict], puntos: list[dict]) -> list[dict]:
    despacho_zonas = {d.get("ZONA_ID") for d in despachos_hoy if d.get("ZONA_ID")}
    puntos_pend_by_zona: dict[str, int] = {}
    for p in puntos:
        if str(p.get("ESTADO", "")).upper() in PENDING_STATES:
            zid = p.get("ZONA_ID")
            if zid:
                puntos_pend_by_zona[zid] = puntos_pend_by_zona.get(zid, 0) + 1

    official_ids = {z.get("ZONA_ID") for z in zonas if z.get("ZONA_ID")}
    by_zone: dict[str, list[dict]] = {}
    for r in enriched:
        zid = r.get("zona_id") or ""  # blank/unmatched -> grouped under "" below
        by_zone.setdefault(zid, []).append(r)

    rows = []
    for z in zonas:
        zid = z.get("ZONA_ID")
        if not zid:
            continue
        criticos = by_zone.get(zid, [])
        n_criticos = len(criticos)
        n_f3 = sum(1 for r in criticos if r["f3_hecha"])
        rows.append({
            "zona_id": zid,
            "comuna": z.get("COMUNA"),
            "lider": z.get("LIDER_ACTUAL"),
            "estado_zona": z.get("ESTADO_ACTUAL"),
            "despacho_hoy": zid in despacho_zonas,
            "n_criticos": n_criticos,
            "n_f3_hechas": n_f3,
            "n_faltantes": n_criticos - n_f3,
            "gestor_n_puntos": int(_num(z.get("N_PUNTOS")) or 0),
            "gestor_pendientes": puntos_pend_by_zona.get(zid, 0),
        })

    # Records whose zona_id is blank or doesn't match any known gestor zone:
    # the summary counts them (total_criticos), so the table needs a row too,
    # or the two never reconcile.
    fuera = [r for zid, recs in by_zone.items() if zid not in official_ids for r in recs]
    if fuera:
        n_criticos = len(fuera)
        n_f3 = sum(1 for r in fuera if r["f3_hecha"])
        rows.append({
            "zona_id": FUERA_DE_ZONA, "comuna": "", "lider": "", "estado_zona": "",
            "despacho_hoy": False, "n_criticos": n_criticos, "n_f3_hechas": n_f3,
            "n_faltantes": n_criticos - n_f3, "gestor_n_puntos": 0, "gestor_pendientes": 0,
        })

    rows.sort(key=lambda r: r["n_faltantes"], reverse=True)
    return rows


def build_cruce(roster_records: list[dict], zonas: list[dict], evaluaciones: list[dict],
                puntos: list[dict], despachos_hoy: list[dict], now: datetime) -> dict:
    enriched = []
    for r in roster_records:
        lat, lon = _num(r.get("lat")), _num(r.get("lon"))
        row = {
            "registro_id": r.get("registro_id"),
            "lat": lat, "lon": lon,
            "direccion": r.get("direccion"),
            "zona_id": r.get("zona_id"),
            "estado_visita": r.get("estado_visita"),
            "score": r.get("score"),
        }
        row.update(cruce_f3(lat, lon, evaluaciones))
        row.update(cruce_punto(lat, lon, puntos))
        enriched.append(row)

    zonas_rollup = build_zona_rollups(zonas, enriched, despachos_hoy, puntos)

    n_f3 = sum(1 for r in enriched if r["f3_hecha"])
    gestor_pend = sum(1 for p in puntos if str(p.get("ESTADO", "")).upper() in PENDING_STATES)
    despacho_zonas = {d.get("ZONA_ID") for d in despachos_hoy if d.get("ZONA_ID")}
    resumen = {
        "total_criticos": len(enriched),
        "f3_hechas": n_f3,
        "f3_faltantes": len(enriched) - n_f3,
        "gestor_puntos_totales": len(puntos),
        "gestor_puntos_pendientes": gestor_pend,
        "zonas_con_despacho_hoy": len(despacho_zonas),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    return {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"), "resumen": resumen,
            "zonas": zonas_rollup, "records": enriched}


# ── Self-check ────────────────────────────────────────────────────────────────
def _selfcheck():
    zonas = [{"ZONA_ID": "Z1", "COMUNA": "Comuna 1", "LIDER_ACTUAL": "Ana",
              "ESTADO_ACTUAL": "ASIGNADA", "N_PUNTOS": 5}]
    despachos_hoy = [{"ZONA_ID": "Z1"}]
    evaluaciones = [{"GLOBAL_ID": "E1", "FECHA_INSPECCION": "2026-08-10", "X": -76.5300,
                     "Y": 3.4300, "SEVERIDAD": "bajo", "HABITABILIDAD": "h"}]
    puntos = [{"ZONA_ID": "Z1", "ESTADO": "PENDIENTE", "LAT": 3.5000, "LON": -76.6000}]
    roster = [
        {"registro_id": "R1", "lat": 3.4300, "lon": -76.5300, "direccion": "CL 1",
         "zona_id": "Z1", "estado_visita": "pendiente", "score": 80.0},   # same point as evaluacion -> hecha
        {"registro_id": "R2", "lat": 3.0000, "lon": -76.0000, "direccion": "CL 2",
         "zona_id": "Z1", "estado_visita": "pendiente", "score": 60.0},   # far -> not hecha
        {"registro_id": "R4", "lat": 3.1000, "lon": -76.1000, "direccion": "CL 4",
         "zona_id": "", "estado_visita": "pendiente", "score": 40.0},     # blank zona_id -> fuera de zona
        {"registro_id": "R5", "lat": 3.2000, "lon": -76.2000, "direccion": "CL 5",
         "zona_id": "Z9", "estado_visita": "pendiente", "score": 30.0},   # unmatched zona_id -> fuera de zona
    ]

    # nearest(): boundary sanity — same-point match, far-point no-match.
    best, dist = nearest(3.4300, -76.5300, evaluaciones, _eval_latlon)
    assert best is not None and dist < 1.0, (best, dist)
    best2, dist2 = nearest(3.0000, -76.0000, evaluaciones, _eval_latlon)
    assert best2 is None and dist2 is None

    now = datetime(2026, 8, 18, 12, 0)
    out = build_cruce(roster, zonas, evaluaciones, puntos, despachos_hoy, now)

    recs = {r["registro_id"]: r for r in out["records"]}
    assert recs["R1"]["f3_hecha"] is True and recs["R1"]["f3_global_id"] == "E1"
    assert recs["R1"]["f3_dist_m"] < 1.0
    assert recs["R2"]["f3_hecha"] is False and recs["R2"]["f3_global_id"] is None
    assert recs["R1"]["punto_estado"] is None  # nearest punto (Z1) is >40m from R1

    assert len(out["zonas"]) == 2  # Z1 + the synthetic "(fuera de zona)" row
    by_id = {z["zona_id"]: z for z in out["zonas"]}
    assert by_id["Z1"] == {"zona_id": "Z1", "comuna": "Comuna 1", "lider": "Ana", "estado_zona": "ASIGNADA",
                           "despacho_hoy": True, "n_criticos": 2, "n_f3_hechas": 1, "n_faltantes": 1,
                           "gestor_n_puntos": 5, "gestor_pendientes": 1}, by_id["Z1"]
    fuera = by_id[FUERA_DE_ZONA]  # R4 (blank zona_id) + R5 (unmatched "Z9"), neither has an F3
    assert fuera == {"zona_id": FUERA_DE_ZONA, "comuna": "", "lider": "", "estado_zona": "",
                     "despacho_hoy": False, "n_criticos": 2, "n_f3_hechas": 0, "n_faltantes": 2,
                     "gestor_n_puntos": 0, "gestor_pendientes": 0}, fuera

    r = out["resumen"]
    assert r["total_criticos"] == 4 and r["f3_hechas"] == 1 and r["f3_faltantes"] == 3
    assert r["gestor_puntos_totales"] == 1 and r["gestor_puntos_pendientes"] == 1
    assert r["zonas_con_despacho_hoy"] == 1
    # Table (zonas rollup) reconciles with the summary card.
    assert sum(z["n_criticos"] for z in out["zonas"]) == r["total_criticos"]
    assert sum(z["n_faltantes"] for z in out["zonas"]) == r["f3_faltantes"]

    # Degraded input: missing lat/lon never crashes, just no match.
    row = {"registro_id": "R3", "lat": None, "lon": None, "direccion": "", "zona_id": "Z1",
           "estado_visita": "pendiente", "score": 0}
    out2 = build_cruce([row], zonas, evaluaciones, puntos, despachos_hoy, now)
    assert out2["records"][0]["f3_hecha"] is False
    print("selfcheck ok")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> dict:
    if "--check" in sys.argv:
        _selfcheck()
        return {}

    roster = json.loads(ASIGNACIONES_JSON.read_text(encoding="utf-8"))
    zonas = fetch_gestor("zonas")
    evaluaciones = fetch_gestor("evaluaciones")
    puntos = fetch_gestor("puntos")
    despachos_hoy = fetch_gestor("despachosHoy")
    print(f"gestor: {len(zonas)} zonas | {len(evaluaciones)} evaluaciones | "
          f"{len(puntos)} puntos | {len(despachos_hoy)} despachos hoy")

    now = datetime.now()
    out = build_cruce(roster["records"], zonas, evaluaciones, puntos, despachos_hoy, now)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    r = out["resumen"]
    print(f"cruce_gestor: {r['total_criticos']} críticos | F3 hechas {r['f3_hechas']} | "
          f"faltantes {r['f3_faltantes']} | zonas {len(out['zonas'])} "
          f"({r['zonas_con_despacho_hoy']} con despacho hoy) | "
          f"puntos gestor {r['gestor_puntos_totales']} (pendientes {r['gestor_puntos_pendientes']}) "
          f"-> {OUT_JSON.relative_to(REPO_ROOT)}")
    return r


if __name__ == "__main__":
    main()
