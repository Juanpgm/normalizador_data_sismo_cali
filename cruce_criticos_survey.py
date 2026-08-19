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
  3. direccion -> direccion normalizada IGAC igual (exacta o difflib >= 0.90)
  4. combinado -> distancia y direccion apuntan al MISMO survey: prefijo (barrio
                  pegado sin coma, <= 150 m) o casi-match (ratio >= 0.85, <= 60 m)
  5. miss      -> sin EDE -> pendiente

Uso:
    python cruce_criticos_survey.py --check      # self-check offline, sin red
    python cruce_criticos_survey.py              # datos reales -> escribe el JSON
    python cruce_criticos_survey.py --firebase   # ...y ademas sube a Firestore
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from difflib import SequenceMatcher

from integracion import api_visitados
from integracion.coords import haversine_m, parse_latlon
# Mismos umbrales y llave de direccion que el cruce contra el Gestor de Zonas.
from cruce_gestor import ADDR_MATCH_RATIO, COMBO_MAX_M, COMBO_RATIO, PREFIX_MAX_M, addr_key
# Reusamos las piezas puras de asignar_f3: llave de integracion, normalizacion de
# globalid y las zonas KML (misma logica de sectores del "app script").
from asignar_f3 import id_asignacion, _norm_globalid, parse_zonas_kml, resolve_kml, write_geojson

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[0]
INSPECTIONS_JSON = REPO_ROOT / "web" / "data" / "inspections.json"
OUT_JSON = REPO_ROOT / "web" / "data" / "cruce_criticos_survey.json"
ZONES_GEOJSON = REPO_ROOT / "web" / "data" / "zonas_asignacion.geojson"

MATCH_MAX_M = 20.0  # radio de match (coords corregidas -> radio apretado y seguro)


def _num_or_none(v):
    """'2' -> 2, ''/None/basura -> None (para víctimas y timestamps de la API)."""
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


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
        direccion = r.get("direccion_norm") or r.get("direccion")
        out.append({
            "globalid": _norm_globalid(r.get("GlobalID")),
            "lat": float(lat), "lon": float(lon),
            "direccion": direccion,
            "akey": addr_key(direccion),
            "fecha": r.get("fecha_inspeccion"),
            "evaluador": r.get("nombre_evaluador"),
        })
    return out


def _match_direccion(direccion, lat, lon, survey: list[dict]):
    """Match por direccion normalizada: (survey, via) o (None, None). Las reglas
    no-exactas exigen que distancia y direccion apunten al MISMO survey, o
    direcciones casi iguales en otra parte de la ciudad dan falso positivo."""
    key = addr_key(direccion)
    if not key:
        return None, None
    for s in survey:
        if s.get("akey") == key:
            return s, "direccion"

    def dist(s):
        if lat is None or lon is None:
            return None
        return haversine_m((lat, lon), (s["lat"], s["lon"]))

    best, best_key, best_ratio = None, "", 0.0
    # ponytail: O(pendientes × survey) SequenceMatcher scan; indexar por via si
    # alguna vez se hace lento.
    for s in survey:
        k = s.get("akey")
        if not k:
            continue
        if len(key) >= 8 and len(k) >= 8 and (k.startswith(key) or key.startswith(k)):
            d = dist(s)
            if d is not None and d <= PREFIX_MAX_M:
                return s, "combinado"
        ratio = SequenceMatcher(None, key, k).ratio()
        if ratio > best_ratio:
            best, best_key, best_ratio = s, k, ratio
    if best is not None and best_ratio >= ADDR_MATCH_RATIO:
        # Fuzzy no-exacto: mismos dígitos (solo cambia formato/letras) o el
        # mismo survey cerca — una placa distinta con ratio alto no es match.
        d = dist(best)
        if (re.sub(r"\D", "", key) == re.sub(r"\D", "", best_key)
                or (d is not None and d <= PREFIX_MAX_M)):
            return best, "direccion"
    if best is not None and best_ratio >= COMBO_RATIO:
        d = dist(best)
        if d is not None and d <= COMBO_MAX_M:
            return best, "combinado"
    return None, None


def match_survey(place_id, lat, lon, direccion, survey: list[dict],
                 by_gid: dict[str, dict]) -> dict:
    """Cascada de match de un critico:
    globalid -> cercania (<=MATCH_MAX_M) -> direccion/combinado -> miss."""
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
    hit, via = _match_direccion(direccion, lat, lon, survey)
    if hit is not None:
        d = (haversine_m((lat, lon), (hit["lat"], hit["lon"]))
             if lat is not None and lon is not None else None)
        return {"estado": "levantado", "match": via,
                "survey_globalid": hit["globalid"],
                "dist_m": round(d) if d is not None else None,
                "survey_fecha": hit["fecha"], "survey_evaluador": hit["evaluador"]}
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
        m = match_survey(c.get("sitio_id"), lat, lon, c.get("direccion_unificada"), survey, by_gid)
        z = zone_for(lon, lat) or {}
        zid = z.get("zone_id") or ""
        rec = {
            "clave_integracion": id_asignacion(rid),
            "registro_id": rid,
            "evaluacion_id": c.get("visita_id"),  # id de la evaluación en la API
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
            # Datos completos del crítico (los que trae la API por evaluación).
            "estado_api": c.get("estado"),                       # estadoEtiqueta
            "habitabilidad": c.get("estado_estructura"),         # habitabilidadEtiqueta
            "tipo_estructura": c.get("tipo_estructura_visita") or c.get("tipo_estructura_edan"),
            "nombre_estructura": c.get("nombre_estructura"),
            "n_fallecidos": _num_or_none(c.get("n_fallecidos_total")),
            "n_atrapamientos": _num_or_none(c.get("n_atrapamientos_total")),
            "n_rescatados": _num_or_none(c.get("n_rescatados_total")),
            "descripcion_edan": c.get("descripcion_edan"),
            "descripcion_visita": c.get("descripcion_visita"),
            "evaluacion_creado_utc": _num_or_none(c.get("evaluacion_creado_utc")),
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
        "por_direccion": sum(1 for r in records if r["match"] == "direccion"),
        "por_combinado": sum(1 for r in records if r["match"] == "combinado"),
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
         "direccion": "Calle 1 # 2-3", "akey": addr_key("Calle 1 # 2-3"),
         "fecha": "2026-08-11", "evaluador": "X"},
        {"globalid": "bbb", "lat": 3.4500, "lon": -76.5600,
         "direccion": "Carrera 9 # 8-7", "akey": addr_key("Carrera 9 # 8-7"),
         "fecha": "2026-08-12", "evaluador": "Y"},
    ]
    by_gid = {s["globalid"]: s for s in survey}
    # 1. globalid exacto
    r = match_survey("arcgis:{AAA}", 3.9, -76.9, "", survey, by_gid)
    assert r["estado"] == "levantado" and r["match"] == "globalid", r
    # 2. cercania: ~11 m del punto aaa
    r = match_survey("ChIJx", 3.42010, -76.53000, "", survey, by_gid)
    assert r["estado"] == "levantado" and r["match"] == "cercania", r
    assert r["dist_m"] <= MATCH_MAX_M, r
    # 3. direccion exacta normalizada, lejos de todo -> levantado igual
    r = match_survey("ChIJx", 3.9, -76.9, "CL 1 No. 2-3, Cali", survey, by_gid)
    assert r["estado"] == "levantado" and r["match"] == "direccion", r
    assert r["survey_globalid"] == "aaa", r
    # 4. combinado: barrio pegado sin coma + mismo survey a ~100 m
    r = match_survey("ChIJx", 3.4209, -76.5300, "CL 1 # 2-3 BARRIO CENTRO", survey, by_gid)
    assert r["estado"] == "levantado" and r["match"] == "combinado", r
    assert r["survey_globalid"] == "aaa" and 50 < r["dist_m"] < 150, r
    # 5. direccion parecida pero lejos -> NO matchea (evidencia en conflicto)
    r = match_survey("ChIJx", 3.9, -76.9, "CL 1 # 2-8", survey, by_gid)
    assert r["estado"] == "pendiente" and r["match"] is None, r
    # 6. miss: lejos de todo, direccion distinta
    r = match_survey("ChIJx", 3.4200, -76.5000, "DG 99 # 1-1", survey, by_gid)
    assert r["estado"] == "pendiente" and r["match"] is None, r
    # 7. sin coords ni direccion -> pendiente, no revienta
    r = match_survey("", None, None, "", survey, by_gid)
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
    out = build_cruce(criticos, survey, zone_for)
    # Artefactos LOCALES (web/data del repo dashboard): opcionales. En Railway el
    # contenedor solo tiene /app y REPO_ROOT apunta a /; ahi el destino real es
    # Firestore, asi que un fallo de disco no debe tumbar la corrida.
    try:
        if zone_feats:
            ZONES_GEOJSON.parent.mkdir(parents=True, exist_ok=True)
            write_geojson(zone_feats, ZONES_GEOJSON)
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        destino_local = str(OUT_JSON)
    except OSError as exc:
        destino_local = f"(sin artefactos locales: {exc})"
    r = out["resumen"]
    print(f"cruce: {r['total_criticos']} criticos | levantados {r['levantados']} "
          f"(globalid {r['por_globalid']} + cercania {r['por_cercania']} + "
          f"direccion {r['por_direccion']} + combinado {r['por_combinado']}) | "
          f"pendientes {r['pendientes']} | survey {r['survey_usados']}/{r['survey_puntos']} usados")
    print(f"-> {destino_local} | zonas KML: {len(zone_feats)} -> {ZONES_GEOJSON.name}")
    if "--firebase" in sys.argv:
        import subir_cruce_firebase as fb
        fb.upload(out, fb.FIRESTORE_PROJECT, fb.COLLECTION, None)  # SA desde config/.env


if __name__ == "__main__":
    main()
