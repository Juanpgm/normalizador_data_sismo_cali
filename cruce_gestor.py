"""Cross-reference ("cruce") the F3 assignment roster against the "Gestor de
Zonas — PMU Cali" Apps Script API: which critical points already have a field
visit (F3) done, which gestor zone each critical point falls in, and a
per-zone rollup of remaining work.

Reads web/data/asignaciones.json (the roster asignar_f3.py exports) and the
gestor's `zonas`, `evaluaciones`, `puntos` and `despachosHoy` endpoints. For
each roster record:

- F3 match: nearest gestor `evaluaciones` row within 40 m (haversine); when
  there is no geo match, falls back to normalized-address matching (IGAC
  normalization, exact then fuzzy) against the evaluacion's DIRECCION.
- Zone: assigned by point-in-polygon against each zona's POLIGONO (the
  authoritative gestor geometry); the roster's zona_id is only a fallback for
  records with no coordinates or outside every polygon.
- Nearest gestor `puntos` row within 40 m (status only).

Writes web/data/cruce_gestor.json: a global resumen, a per-zone rollup
(faltantes desc) and the enriched roster records.

    python cruce_gestor.py --check   # offline self-check, no network
    python cruce_gestor.py           # real data, write web/data/cruce_gestor.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import requests

from integracion.coords import haversine_m
from integracion.normalization import normalize_address

BASE_URL = ("https://script.google.com/macros/s/"
            "AKfycbw4uRh_HkiZtKgvb1ZkGYt_m5wydNFWw1SkN6D2ttGEaLm_V4cHOJCqg5I581hqevuP/exec")
MAX_MATCH_M = 40.0
ADDR_MATCH_RATIO = 0.90
COMBO_MAX_M = 60.0     # casi-match geo con dirección fuerte (evidencia combinada)
COMBO_RATIO = 0.85
PREFIX_MAX_M = 150.0   # misma dirección con sufijo pegado (barrio sin coma)
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


import re as _re

_CARDINALES = [(_re.compile(r"\bNORTE\b|\bNTE\.?\b"), "N"),
               (_re.compile(r"\bOESTE\b|\bOE\.?\b"), "O"),
               (_re.compile(r"\bESTE\b"), "E"),
               (_re.compile(r"\bSUR\b"), "S")]


def addr_key(direccion) -> str:
    """Comparable address key: IGAC-normalized nomenclature, complement (after
    the first comma: barrio/ciudad suffixes) dropped, cardinal words collapsed
    to their single-letter suffix (NORTE -> N), uppercased."""
    s = normalize_address(direccion).split(",")[0].strip().upper()
    for pat, letter in _CARDINALES:
        s = pat.sub(letter, s)
    s = _re.sub(r"(\d) ([NSEO])\b", r"\1\2", s)  # "63 N" -> "63N"
    return _re.sub(r"\s+", " ", s).strip()


def build_addr_index(evaluaciones: list[dict]) -> list[tuple[str, dict]]:
    """(addr_key, evaluacion) pairs for every evaluacion with a usable DIRECCION."""
    out = []
    for e in evaluaciones:
        k = addr_key(e.get("DIRECCION"))
        if k:
            out.append((k, e))
    return out


def _dist_to(lat, lon, e: dict):
    ll = _eval_latlon(e)
    if ll is None or lat is None or lon is None:
        return None
    return haversine_m((lat, lon), ll)


def match_by_direccion(lat, lon, direccion, addr_index: list[tuple[str, dict]]):
    """Best evaluacion by normalized address. Returns (evaluacion, via, dist_m)
    or (None, None, None). Acceptance ladder — every rule below the exact one
    demands that DISTANCE and ADDRESS point at the SAME evaluacion, or near-
    identical addresses at different points of the city false-positive:

    - exacta:    same normalized key                      -> "direccion"
    - fuzzy:     ratio >= 0.90                            -> "direccion"
    - prefijo:   one key is a prefix of the other (barrio
                 glued without comma) and that evaluacion
                 is <= 150 m away                         -> "combinado"
    - combinado: ratio >= 0.85 and that evaluacion is
                 <= 60 m away (near-miss on both axes)    -> "combinado"
    """
    key = addr_key(direccion)
    if not key:
        return None, None, None
    for k, e in addr_index:
        if k == key:
            return e, "direccion", _dist_to(lat, lon, e)
    best, best_ratio = None, 0.0
    # ponytail: O(roster_sin_geo × evaluaciones) SequenceMatcher scan; index by
    # via+numero if it ever gets slow.
    for k, e in addr_index:
        if len(key) >= 8 and len(k) >= 8 and (k.startswith(key) or key.startswith(k)):
            d = _dist_to(lat, lon, e)
            if d is not None and d <= PREFIX_MAX_M:
                return e, "combinado", d
        ratio = SequenceMatcher(None, key, k).ratio()
        if ratio > best_ratio:
            best, best_ratio = e, ratio
    if best is not None and best_ratio >= ADDR_MATCH_RATIO:
        return best, "direccion", _dist_to(lat, lon, best)
    if best is not None and best_ratio >= COMBO_RATIO:
        d = _dist_to(lat, lon, best)
        if d is not None and d <= COMBO_MAX_M:
            return best, "combinado", d
    return None, None, None


def cruce_f3(lat, lon, direccion, evaluaciones: list[dict],
             addr_index: list[tuple[str, dict]]) -> dict:
    """F3 visit already done: nearest within 40 m, or (fallback) address-based
    match (see match_by_direccion). f3_match_via says which criterion matched."""
    best, dist = nearest(lat, lon, evaluaciones, _eval_latlon)
    via = "geo" if best is not None else None
    if best is None:
        best, via, dist = match_by_direccion(lat, lon, direccion, addr_index)
    if best is None:
        return {"f3_hecha": False, "f3_global_id": None, "f3_fecha": None,
                "f3_severidad": None, "f3_habitabilidad": None, "f3_dist_m": None,
                "f3_match_via": None}
    return {"f3_hecha": True, "f3_global_id": best.get("GLOBAL_ID"),
            "f3_fecha": best.get("FECHA_INSPECCION"), "f3_severidad": best.get("SEVERIDAD"),
            "f3_habitabilidad": best.get("HABITABILIDAD"),
            "f3_dist_m": round(dist, 1) if dist is not None else None,
            "f3_match_via": via}


def cruce_punto(lat, lon, puntos: list[dict]) -> dict:
    """Nearest gestor `puntos` entry within 40 m (its status)."""
    best, _ = nearest(lat, lon, puntos, _punto_latlon)
    return {"punto_estado": best.get("ESTADO") if best else None}


# ── Zone by polygon ─────────────────────────────────────────────────────────
def _zona_poly(z: dict):
    """POLIGONO as [[lat, lon], ...], parsed from JSON string or passed-through
    list. None when absent/malformed."""
    raw = z.get("POLIGONO")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, list) or len(raw) < 3:
        return None
    return raw


def point_in_poly(lat, lon, poly) -> bool:
    """Ray casting over [[lat, lon], ...] vertices."""
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i][0], poly[i][1]
        yj, xj = poly[j][0], poly[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def build_zona_polys(zonas: list[dict]) -> list[tuple[str, list]]:
    return [(z["ZONA_ID"], poly) for z in zonas
            if z.get("ZONA_ID") and (poly := _zona_poly(z)) is not None]


def zona_por_poligono(lat, lon, zona_polys: list[tuple[str, list]]) -> str | None:
    if lat is None or lon is None:
        return None
    for zid, poly in zona_polys:
        if point_in_poly(lat, lon, poly):
            return zid
    return None


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
    addr_index = build_addr_index(evaluaciones)
    zona_polys = build_zona_polys(zonas)
    enriched = []
    for r in roster_records:
        lat, lon = _num(r.get("lat")), _num(r.get("lon"))
        # Zona por POLÍGONO del gestor (autoritativa); el zona_id del roster es
        # solo fallback (sin coords o fuera de todo polígono).
        zid_poly = zona_por_poligono(lat, lon, zona_polys)
        row = {
            "registro_id": r.get("registro_id"),
            "lat": lat, "lon": lon,
            "direccion": r.get("direccion"),
            "zona_id": zid_poly or r.get("zona_id"),
            "zona_via": "poligono" if zid_poly else ("roster" if r.get("zona_id") else None),
            "estado_visita": r.get("estado_visita"),
            "score": r.get("score"),
        }
        row.update(cruce_f3(lat, lon, r.get("direccion"), evaluaciones, addr_index))
        row.update(cruce_punto(lat, lon, puntos))
        enriched.append(row)

    zonas_rollup = build_zona_rollups(zonas, enriched, despachos_hoy, puntos)

    n_f3 = sum(1 for r in enriched if r["f3_hecha"])
    gestor_pend = sum(1 for p in puntos if str(p.get("ESTADO", "")).upper() in PENDING_STATES)
    despacho_zonas = {d.get("ZONA_ID") for d in despachos_hoy if d.get("ZONA_ID")}
    resumen = {
        "total_criticos": len(enriched),
        "f3_hechas": n_f3,
        "f3_por_direccion": sum(1 for r in enriched if r.get("f3_match_via") == "direccion"),
        "f3_por_combinado": sum(1 for r in enriched if r.get("f3_match_via") == "combinado"),
        "zona_por_poligono": sum(1 for r in enriched if r.get("zona_via") == "poligono"),
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
    z1_poly = "[[3.42, -76.54], [3.44, -76.54], [3.44, -76.52], [3.42, -76.52]]"
    zonas = [{"ZONA_ID": "Z1", "COMUNA": "Comuna 1", "LIDER_ACTUAL": "Ana",
              "ESTADO_ACTUAL": "ASIGNADA", "N_PUNTOS": 5, "POLIGONO": z1_poly}]
    despachos_hoy = [{"ZONA_ID": "Z1"}]
    evaluaciones = [{"GLOBAL_ID": "E1", "FECHA_INSPECCION": "2026-08-10", "X": -76.5300,
                     "Y": 3.4300, "SEVERIDAD": "bajo", "HABITABILIDAD": "h",
                     "DIRECCION": "Calle 1 # 2-3"}]
    puntos = [{"ZONA_ID": "Z1", "ESTADO": "PENDIENTE", "LAT": 3.5000, "LON": -76.6000}]
    roster = [
        {"registro_id": "R1", "lat": 3.4300, "lon": -76.5300, "direccion": "CL 1",
         "zona_id": "", "estado_visita": "pendiente", "score": 80.0},     # same point as evaluacion -> hecha (geo); dentro del polígono Z1
        {"registro_id": "R2", "lat": 3.0000, "lon": -76.0000, "direccion": "CL 2",
         "zona_id": "Z1", "estado_visita": "pendiente", "score": 60.0},   # far, address distinta -> not hecha; zona por roster
        {"registro_id": "R4", "lat": 3.1000, "lon": -76.1000, "direccion": "CL 4",
         "zona_id": "", "estado_visita": "pendiente", "score": 40.0},     # blank zona_id -> fuera de zona
        {"registro_id": "R5", "lat": 3.2000, "lon": -76.2000, "direccion": "CL 5",
         "zona_id": "Z9", "estado_visita": "pendiente", "score": 30.0},   # unmatched zona_id -> fuera de zona
        {"registro_id": "R6", "lat": 3.3000, "lon": -76.3000, "direccion": "Calle 1 No. 2-3, Cali",
         "zona_id": "", "estado_visita": "pendiente", "score": 20.0},     # far pero misma dirección normalizada -> hecha (direccion)
        {"registro_id": "R7", "lat": 3.4309, "lon": -76.5300, "direccion": "CL 1 # 2-3 BARRIO CENTRO",
         "zona_id": "", "estado_visita": "pendiente", "score": 10.0},     # ~100m + dirección con barrio pegado -> hecha (combinado)
    ]

    # nearest(): boundary sanity — same-point match, far-point no-match.
    best, dist = nearest(3.4300, -76.5300, evaluaciones, _eval_latlon)
    assert best is not None and dist < 1.0, (best, dist)
    best2, dist2 = nearest(3.0000, -76.0000, evaluaciones, _eval_latlon)
    assert best2 is None and dist2 is None

    # Address key + polygon sanity.
    assert addr_key("Calle 1 No. 2-3, Cali") == addr_key("CL 1 # 2-3") == "CL 1 # 2-3"
    assert addr_key("CL 63 NORTE # 3EN-60") == addr_key("Calle 63 N # 3EN-60") == "CL 63N # 3EN-60"
    poly = _zona_poly(zonas[0])
    assert point_in_poly(3.43, -76.53, poly) is True
    assert point_in_poly(3.00, -76.00, poly) is False

    now = datetime(2026, 8, 18, 12, 0)
    out = build_cruce(roster, zonas, evaluaciones, puntos, despachos_hoy, now)

    recs = {r["registro_id"]: r for r in out["records"]}
    assert recs["R1"]["f3_hecha"] is True and recs["R1"]["f3_global_id"] == "E1"
    assert recs["R1"]["f3_dist_m"] < 1.0 and recs["R1"]["f3_match_via"] == "geo"
    assert recs["R1"]["zona_id"] == "Z1" and recs["R1"]["zona_via"] == "poligono"  # roster venía vacío
    assert recs["R2"]["f3_hecha"] is False and recs["R2"]["f3_global_id"] is None
    assert recs["R2"]["zona_id"] == "Z1" and recs["R2"]["zona_via"] == "roster"    # fuera del polígono
    assert recs["R6"]["f3_hecha"] is True and recs["R6"]["f3_match_via"] == "direccion"
    assert recs["R6"]["f3_global_id"] == "E1" and recs["R6"]["f3_dist_m"] > 1000  # exacta reporta la distancia igual
    assert recs["R7"]["f3_hecha"] is True and recs["R7"]["f3_match_via"] == "combinado"
    assert recs["R7"]["f3_global_id"] == "E1" and 50 < recs["R7"]["f3_dist_m"] < 150
    assert recs["R7"]["zona_via"] == "poligono"
    assert recs["R1"]["punto_estado"] is None  # nearest punto (Z1) is >40m from R1

    assert len(out["zonas"]) == 2  # Z1 + the synthetic "(fuera de zona)" row
    by_id = {z["zona_id"]: z for z in out["zonas"]}
    assert by_id["Z1"] == {"zona_id": "Z1", "comuna": "Comuna 1", "lider": "Ana", "estado_zona": "ASIGNADA",
                           "despacho_hoy": True, "n_criticos": 3, "n_f3_hechas": 2, "n_faltantes": 1,
                           "gestor_n_puntos": 5, "gestor_pendientes": 1}, by_id["Z1"]
    fuera = by_id[FUERA_DE_ZONA]  # R4 (blank) + R5 ("Z9") + R6 (blank, F3 por dirección)
    assert fuera == {"zona_id": FUERA_DE_ZONA, "comuna": "", "lider": "", "estado_zona": "",
                     "despacho_hoy": False, "n_criticos": 3, "n_f3_hechas": 1, "n_faltantes": 2,
                     "gestor_n_puntos": 0, "gestor_pendientes": 0}, fuera

    r = out["resumen"]
    assert r["total_criticos"] == 6 and r["f3_hechas"] == 3 and r["f3_faltantes"] == 3
    assert r["f3_por_direccion"] == 1 and r["f3_por_combinado"] == 1 and r["zona_por_poligono"] == 2
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
    assert out2["records"][0]["zona_via"] == "roster"
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
