"""Cruce puntos_criticos (API atencionsismo) <-> puntos_survey (EDE normalizado).

Lee los criticos de la API `visitados-criticos` (via integracion.api_visitados) y
los cruza contra el survey normalizado con COORDENADAS CORREGIDAS que vive en
`web/data/inspections.json` (x/y ya corregidos por EXIF/geocode en refresh_data).

Para cada critico decide:
  * levantado  -> tiene un registro EDE que le corresponde (ya lo visitaron en campo)
  * pendiente  -> no hay EDE cerca -> falta, es asignable

y le pone una llave de integracion estable (`clave_integracion`) que sobrevive
corridas. Bucketiza por sector con la misma logica de zonas KML de asignar_f3.

Cascada de match, radio MATCH_MAX_M sobre coords corregidas:
  1. globalid  -> placeId 'arcgis:<globalid>' del critico == GlobalID del survey
  2. cercania  -> survey mas cercano dentro de MATCH_MAX_M (haversine)
  3. miss      -> sin EDE -> pendiente

Uso:
    python cruce_criticos_survey.py --check      # self-check offline, sin red
    python cruce_criticos_survey.py              # datos reales -> escribe el JSON
    python cruce_criticos_survey.py --firebase   # ...y ademas sube a Firestore
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from integracion import api_visitados
from integracion.coords import haversine_m, parse_latlon
# Reusamos las piezas puras de asignar_f3: llave de integracion, normalizacion de
# globalid y las zonas KML (misma logica de sectores del "app script").
from asignar_f3 import id_asignacion, _norm_globalid, parse_zonas_kml, resolve_kml, write_geojson

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[0]
INSPECTIONS_JSON = REPO_ROOT / "web" / "data" / "inspections.json"
OUT_JSON = REPO_ROOT / "web" / "data" / "cruce_criticos_survey.json"
ZONES_GEOJSON = REPO_ROOT / "web" / "data" / "zonas_asignacion.geojson"

MATCH_MAX_M = 20.0  # radio de match (coords corregidas -> radio apretado y seguro)


# -- survey normalizado (coords corregidas) -----------------------------------
def load_survey(path: Path = INSPECTIONS_JSON) -> list[dict]:
    """Puntos EDE de inspections.json (survey normalizado con coords CORREGIDAS).
    Lee el archivo local; si no existe (p.ej. en Railway, que solo despliega este
    subproyecto), lo baja de $INSPECTIONS_URL (el JSON publicado en Vercel). Cada
    item: globalid normalizado, lat/lon (y/x), direccion_norm, fecha, evaluador."""
    if path.exists():
        recs = json.loads(path.read_text(encoding="utf-8"))
    else:
        url = os.environ.get("INSPECTIONS_URL", "").strip()
        if not url:
            raise RuntimeError(f"{path} no existe y no hay $INSPECTIONS_URL para bajarlo.")
        recs = requests.get(url, timeout=60).json()
    out = []
    for r in recs:
        lat, lon = r.get("y"), r.get("x")
        if lat in (None, "") or lon in (None, ""):
            continue
        out.append({
            "globalid": _norm_globalid(r.get("GlobalID")),
            "lat": float(lat), "lon": float(lon),
            "direccion": r.get("direccion_norm") or r.get("direccion"),
            "fecha": r.get("fecha_inspeccion"),
            "evaluador": r.get("nombre_evaluador"),
        })
    return out


def match_survey(place_id, lat, lon, survey: list[dict],
                 by_gid: dict[str, dict]) -> dict:
    """Cascada de match de un critico: globalid -> cercania (<=MATCH_MAX_M) -> miss."""
    pid = str(place_id or "")
    if pid.startswith("arcgis:"):
        hit = by_gid.get(_norm_globalid(pid[len("arcgis:"):]))
        if hit is not None:
            return {"estado": "levantado", "match": "globalid",
                    "survey_globalid": hit["globalid"], "dist_m": None,
                    "survey_fecha": hit["fecha"], "survey_evaluador": hit["evaluador"]}
    best, best_d = None, None
    if lat is not None and lon is not None:
        for s in survey:
            d = haversine_m((lat, lon), (s["lat"], s["lon"]))
            if d <= MATCH_MAX_M and (best_d is None or d < best_d):
                best, best_d = s, d
    if best is not None:
        return {"estado": "levantado", "match": "cercania",
                "survey_globalid": best["globalid"], "dist_m": round(best_d),
                "survey_fecha": best["fecha"], "survey_evaluador": best["evaluador"]}
    return {"estado": "pendiente", "match": None, "survey_globalid": None,
            "dist_m": None, "survey_fecha": None, "survey_evaluador": None}


# -- sectores (zonas KML) ------------------------------------------------------
def load_zone_lookup():
    """Devuelve (zone_for, feats): el closure de punto-en-poligono y las features
    GeoJSON de las zonas leidas de la base KML. Best-effort: sin KML -> (no-op, [])."""
    try:
        from shapely.geometry import Point, shape
        from shapely.strtree import STRtree
        feats = parse_zonas_kml(resolve_kml())
    except Exception:  # sin KML / sin shapely -> sectores en blanco, no rompe el cruce
        return (lambda lon, lat: None), []
    if not feats:
        return (lambda lon, lat: None), []
    geoms = [shape(f["geometry"]) for f in feats]
    props = [f["properties"] for f in feats]
    tree = STRtree(geoms)

    def zone_for(lon, lat):
        if lon is None or lat is None:
            return None
        p = Point(lon, lat)
        for idx in tree.query(p, predicate="intersects"):
            if geoms[idx].covers(p):
                return props[idx]
        return None

    return zone_for, feats


# -- cruce ---------------------------------------------------------------------
def build_cruce(criticos, survey: list[dict], zone_for) -> dict:
    by_gid = {s["globalid"]: s for s in survey if s["globalid"]}
    records, zonas = [], {}
    for c in criticos:
        rid = str(c.get("registro_id") or "").strip()
        latlon = parse_latlon(c.get("coords_unificadas"))
        lat, lon = (latlon if latlon else (None, None))
        m = match_survey(c.get("sitio_id"), lat, lon, survey, by_gid)
        z = zone_for(lon, lat) or {}
        zid = z.get("zone_id") or ""
        rec = {
            "clave_integracion": id_asignacion(rid),
            "registro_id": rid,
            "estado": m["estado"],
            "match": m["match"],
            "survey_globalid": m["survey_globalid"],
            "dist_m": m["dist_m"],
            "direccion": c.get("direccion_unificada"),
            "comuna": c.get("comuna_unificada"),
            "barrio": c.get("barrio_unificado"),
            "lat": lat, "lon": lon,
            "nivel_riesgo": c.get("nivel_riesgo"),
            "requiere_demolicion": c.get("requiere_demolicion"),
            "zona_id": zid, "ola": z.get("ola", ""), "despacho": z.get("despacho", ""),
            "survey_fecha": m["survey_fecha"],
            "survey_evaluador": m["survey_evaluador"],
        }
        records.append(rec)
        b = zonas.setdefault(zid or "(fuera de zona)",
                             {"zona_id": zid, "ola": z.get("ola", ""),
                              "despacho": z.get("despacho", ""),
                              "n_criticos": 0, "n_levantados": 0, "n_pendientes": 0})
        b["n_criticos"] += 1
        b["n_levantados" if m["estado"] == "levantado" else "n_pendientes"] += 1

    levantados = sum(1 for r in records if r["estado"] == "levantado")
    resumen = {
        "total_criticos": len(records),
        "levantados": levantados,
        "pendientes": len(records) - levantados,
        "por_globalid": sum(1 for r in records if r["match"] == "globalid"),
        "por_cercania": sum(1 for r in records if r["match"] == "cercania"),
        "survey_puntos": len(survey),
        "survey_usados": len({r["survey_globalid"] for r in records if r["survey_globalid"]}),
    }
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "match_radio_m": MATCH_MAX_M,
        "resumen": resumen,
        "zonas": sorted(zonas.values(), key=lambda z: (-z["n_criticos"], z["zona_id"])),
        "records": records,
    }


def selfcheck() -> None:
    """Offline: valida la cascada de match sin red ni archivos."""
    survey = [
        {"globalid": "aaa", "lat": 3.4200, "lon": -76.5300,
         "direccion": "cll 1", "fecha": "2026-08-11", "evaluador": "X"},
        {"globalid": "bbb", "lat": 3.4500, "lon": -76.5600,
         "direccion": "cll 2", "fecha": "2026-08-12", "evaluador": "Y"},
    ]
    by_gid = {s["globalid"]: s for s in survey}
    # 1. globalid exacto
    r = match_survey("arcgis:{AAA}", 3.9, -76.9, survey, by_gid)
    assert r["estado"] == "levantado" and r["match"] == "globalid", r
    # 2. cercania: ~11 m del punto aaa
    r = match_survey("ChIJx", 3.42010, -76.53000, survey, by_gid)
    assert r["estado"] == "levantado" and r["match"] == "cercania", r
    assert r["dist_m"] <= MATCH_MAX_M, r
    # 3. miss: lejos de todo
    r = match_survey("ChIJx", 3.4200, -76.5000, survey, by_gid)
    assert r["estado"] == "pendiente" and r["match"] is None, r
    # 4. sin coords -> pendiente, no revienta
    r = match_survey("", None, None, survey, by_gid)
    assert r["estado"] == "pendiente", r
    print("cruce_criticos_survey self-check OK")


def main() -> None:
    if "--check" in sys.argv:
        selfcheck()
        return
    survey = load_survey()
    if not survey:
        raise RuntimeError(f"survey vacio en {INSPECTIONS_JSON}; corre refresh_data primero.")
    criticos = api_visitados.fetch_tabla().to_dict("records")
    if not criticos:
        raise RuntimeError("la API devolvio 0 criticos; abortando (no sobrescribo el cruce).")
    zone_for, zone_feats = load_zone_lookup()
    # Las zonas del dashboard salen de la MISMA base KML que usa el cruce.
    if zone_feats:
        write_geojson(zone_feats, ZONES_GEOJSON)
    out = build_cruce(criticos, survey, zone_for)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    r = out["resumen"]
    print(f"cruce: {r['total_criticos']} criticos | levantados {r['levantados']} "
          f"(globalid {r['por_globalid']} + cercania {r['por_cercania']}) | "
          f"pendientes {r['pendientes']} | survey {r['survey_usados']}/{r['survey_puntos']} usados")
    print(f"-> {OUT_JSON} | zonas KML: {len(zone_feats)} -> {ZONES_GEOJSON.name}")
    if "--firebase" in sys.argv:
        import subir_cruce_firebase as fb
        fb.upload(out, fb.FIRESTORE_PROJECT, fb.COLLECTION, None)  # SA desde config/.env


if __name__ == "__main__":
    main()
