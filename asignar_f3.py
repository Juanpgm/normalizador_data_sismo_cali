"""Prioritized F3 assignments from tabla_integrada + KML assignment zones.

Reads `tabla_integrada` (EDAN SISMO sheet) and the `integracion_f3` tab and
builds a persistent assignment roster: every geolocated registro is tagged
`visitado` (already has an F3 in `integracion_f3`) or `pendiente`, and each is
placed in an assignment-zone polygon from the KML priorization map. Pendientes
are scored with a null-safe weighted sum and only the top-N ranked ones are
kept; visitados are all kept so the dashboard shows visited-vs-pending progress.

The score blends NSR-10/AIS structural severity of the reported damage (via the
knowledge graph in `knowledge/kg.json`), a graded victim factor (fatalities >
trapped > rescued, saturating), record age, risk level, demolition flag, and
zone wave. Every component degrades to 0 when its field is missing.

Outputs: the `asignaciones` tab of the EDAN-F3 spreadsheet, plus static
dashboard artifacts `web/data/asignaciones.json` and
`web/data/zonas_asignacion.geojson`. All source tabs are read-only.

    python asignar_f3.py --check     # offline self-check, no network
    python asignar_f3.py --dry       # real data, write output/asignaciones.xlsx only
    python asignar_f3.py             # real data, write the asignaciones tab
    python asignar_f3.py --top 150   # change the number of ranked rows (default 100)
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import gspread
import pandas as pd
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from integracion.config import (EDAN_SPREADSHEET_ID, VISITAS_SHEET_NAME,
                                VISITAS_SPREADSHEET_ID)
from integracion.coords import parse_latlon
from integracion.gauth import credentials

F3_SPREADSHEET_ID = "19k--nAEScol_3E7nbSpPev07gW2_UT8ojSsaMGbn6Ds"
INTEGRADA_TAB = "tabla_integrada"
F3_MATCH_TAB = "integracion_f3"
DST_TAB = "asignaciones"
READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
WRITE = ["https://www.googleapis.com/auth/spreadsheets"]

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[0]
# KML search order: $ZONAS_KML → repo-root basemaps (dev) → vendored basemaps
# (container). Glob a "prioriz" pattern so the dated filename can change without
# a code edit; newest name wins.
KML_DIRS = [REPO_ROOT / "basemaps", HERE / "basemaps"]
KML_NS = "{http://www.opengis.net/kml/2.2}"


def resolve_kml() -> Path:
    env = os.environ.get("ZONAS_KML", "").strip()
    if env:
        return Path(env)
    for d in KML_DIRS:
        hits = sorted(d.glob("*rioriz*.kml"))
        if hits:
            return hits[-1]
    raise FileNotFoundError(
        f"No encontré el KML de priorización (busqué en {[str(d) for d in KML_DIRS]}; "
        "o definí ZONAS_KML)")
ZONES_FOLDER_HINT = "Zonas de asignaci"  # accent-safe prefix

# Tuning knob: component weights of the 0-100 priority score.
WEIGHTS = {
    "grafo_severidad": 30,     # NSR-10/AIS structural severity of the reported damage
    "victimas": 25,            # graded: 3·fallecidos + 2·atrapamientos + 1·rescatados
    "antiguedad": 20,          # days since the Google Forms timestamp, min-max normalized
    "nivel_riesgo": 10,        # Alto=1.0 Medio=0.6 Bajo=0.3
    "requiere_demolicion": 10,
    "ola_zona": 5,             # KML zone wave: OLA 1=1.0 OLA 2=0.5
}

# Victim severity saturates: weighted-count -> [0,1). 1 fatality -> 0.78,
# 2 -> 0.95. ponytail: smooth curve, no batch dependency, tunable via decay.
VICTIM_DECAY = 0.6

# Free-text columns of tabla_integrada scanned for graph pathology terms.
DAMAGE_TEXT_COLS = ("descripcion_edan", "descripcion_visita",
                    "estado_estructura", "nombre_estructura")

OUT_COLS = ["prioridad", "estado_visita", "id_asignacion", "score", "registro_id",
            "direccion", "comuna_corregimiento", "barrio_vereda", "coords",
            "zona_id", "ola", "despacho", "nivel_riesgo", "estado_estructura",
            "grafo_severidad", "n_fallecidos", "n_atrapamientos", "n_rescatados",
            "requiere_demolicion", "antiguedad_dias", "timestamp_registro",
            "flags", "fecha_corrida"]
# Extra columns kept on the frame for the web export (lat/lon to plot points);
# not written to the Google Sheet, which uses OUT_COLS only.
WEB_EXTRA_COLS = ["lat", "lon"]


# ── KML zones ─────────────────────────────────────────────────────────────────
def _parse_desc(desc: str) -> dict:
    """Best-effort extraction from the zone description CDATA; empty on miss."""
    def grab(pattern, group=1):
        m = re.search(pattern, desc, re.IGNORECASE)
        return m.group(group).strip() if m else ""

    return {
        "ola": grab(r"OLA\s*(\d+)"),
        "despacho": grab(r"despacho\s*#?\s*(\d+)"),
        "comuna": grab(r"(Comuna\s*\d+|Corregimiento[^<·•]*)"),
        "barrios": grab(r"Barrios:\s*([^<]+)"),
        "carga": grab(r"Carga\s*([\d.]+)"),
    }


def parse_zonas_kml(path: Path) -> list[dict]:
    """GeoJSON features for every polygon Placemark in the assignment folder."""
    root = ElementTree.parse(path).getroot()
    features = []
    for folder in root.iter(f"{KML_NS}Folder"):
        name = folder.findtext(f"{KML_NS}name") or ""
        if ZONES_FOLDER_HINT not in name:
            continue
        for pm in folder.iter(f"{KML_NS}Placemark"):
            zone_id = (pm.findtext(f"{KML_NS}name") or "").strip()
            polys = []
            for poly in pm.iter(f"{KML_NS}Polygon"):
                ring = poly.find(f"{KML_NS}outerBoundaryIs/{KML_NS}LinearRing/"
                                 f"{KML_NS}coordinates")
                if ring is None or not (ring.text or "").strip():
                    continue
                coords = []
                for token in ring.text.split():
                    parts = token.split(",")
                    if len(parts) >= 2:
                        coords.append([float(parts[0]), float(parts[1])])
                if len(coords) >= 3:
                    polys.append([coords])
            if not zone_id or not polys:
                continue
            geometry = ({"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1
                        else {"type": "MultiPolygon", "coordinates": [p for p in polys]})
            props = {"zone_id": zone_id,
                     **_parse_desc(pm.findtext(f"{KML_NS}description") or "")}
            features.append({"type": "Feature", "properties": props, "geometry": geometry})
    return features


def write_geojson(features: list[dict], path: Path) -> None:
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features},
                               ensure_ascii=False), encoding="utf-8")


# ── Scoring components (all null-safe: unknown/missing -> 0.0) ────────────────
def riesgo_value(v) -> float:
    s = str(v or "").lower()
    if "alto" in s:
        return 1.0
    if "medio" in s:
        return 0.6
    if "bajo" in s:
        return 0.3
    return 0.0


# ── Graph-grounded structural severity (NSR-10 / AIS knowledge graph) ─────────
# Chain: term regex --detecta--> pathology --indica(peso)--> normative criterion.
# The `peso` of the pathology->criterion edge is the NSR-10/AIS severity of the
# damage (fisura 0.25 ... aplastamiento/pandeo/desplome 0.9). graph_severity
# returns the max peso among pathologies whose detection regex fires on the text.
KG_DIRS = [REPO_ROOT / "knowledge", HERE / "knowledge"]


def _resolve_kg() -> Path | None:
    env = os.environ.get("KG_JSON", "").strip()
    if env:
        return Path(env)
    for d in KG_DIRS:
        p = d / "kg.json"
        if p.exists():
            return p
    return None


@functools.lru_cache(maxsize=1)
def _graph_terms() -> list[tuple[re.Pattern, float]]:
    """(compiled term regex, severity peso) pairs, sorted by peso desc so the
    first regex match is the worst pathology. Empty when kg.json is absent."""
    path = _resolve_kg()
    if path is None:
        print("WARN: kg.json no encontrado; grafo_severidad=0 para todos")
        return []
    kg = json.loads(path.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in kg.get("nodes", [])}
    sev: dict[str, float] = {}          # pathology id -> max 'indica' peso
    detecta: dict[str, str] = {}        # term id -> pathology id
    for e in kg.get("edges", []):
        rel = e.get("relacion")
        if rel == "indica":
            sev[e["de"]] = max(sev.get(e["de"], 0.0), float(e.get("peso", 0) or 0))
        elif rel == "detecta":
            detecta[e["de"]] = e["a"]
    terms = []
    for tid, pid in detecta.items():
        patron = nodes.get(tid, {}).get("patron")
        if patron and pid in sev:       # skip element terms (no severity edge)
            terms.append((re.compile(patron, re.IGNORECASE), sev[pid]))
    terms.sort(key=lambda t: t[1], reverse=True)
    return terms


def graph_severity(text) -> float:
    """Max NSR-10/AIS severity peso of any pathology detected in the damage text;
    0.0 when nothing matches (null-safe)."""
    s = str(text or "")
    if not s.strip():
        return 0.0
    for pat, w in _graph_terms():
        if pat.search(s):
            return w
    return 0.0


def victim_factor(fallecidos: float, atrapamientos: float, rescatados: float) -> float:
    """Graded, saturating victim severity in [0,1). Fatalities weigh most; the
    curve rewards any casualty strongly without a hard batch-relative cap.
    (heridos are not in tabla_integrada; atrapados/rescatados are the proxy.)"""
    w = 3.0 * fallecidos + 2.0 * atrapamientos + 1.0 * rescatados
    return 1.0 - VICTIM_DECAY ** w if w > 0 else 0.0


def demolicion_value(v) -> float:
    return 1.0 if str(v or "").strip().lower().startswith(("si", "sí")) else 0.0


def ola_value(v) -> float:
    s = str(v or "").strip()
    return {"1": 1.0, "2": 0.5}.get(s, 0.0)


_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def id_asignacion(key: str) -> str:
    """Deterministic 5-char [0-9A-Z] id from registro_id (same scheme as
    refresh_data._id_edan), so a point keeps its id across runs."""
    n = int(hashlib.sha1(str(key).encode("utf-8")).hexdigest(), 16)
    out = []
    while n:
        n, rem = divmod(n, 36)
        out.append(_ID_ALPHABET[rem])
    return "".join(reversed(out))[:5].rjust(5, "0")


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def normalize_comuna(v) -> str:
    """Canonicalize comuna to 'Comuna N'. tabla_integrada.comuna_unificada comes
    from raw form text, so a bare number ('19') or any casing ('comuna19',
    'COMUNA 5') is normalized; corregimientos and other free text pass through."""
    s = str(v or "").strip()
    if not s:
        return ""
    m = re.fullmatch(r"(?:comuna\s*)?(\d{1,2})", s, re.IGNORECASE)
    return f"Comuna {int(m.group(1))}" if m else s


# ── Core build ────────────────────────────────────────────────────────────────
def f3_done_registros(df_f3_match: pd.DataFrame) -> set[str]:
    """registro_ids that already have an F3 (same derivation as integrar_f3)."""
    if df_f3_match.empty or not {"match_method", "registro_id"}.issubset(df_f3_match.columns):
        return set()
    pairs = ~df_f3_match["match_method"].isin(["", "solo_f3"])
    rids = df_f3_match.loc[pairs, "registro_id"].astype(str).str.rsplit("-", n=1).str[0]
    return set(rids[rids.str.strip().ne("")])


def build_asignaciones(df_integrada: pd.DataFrame, done: set[str],
                       zones: list[dict], ts_by_visita: dict[str, pd.Timestamp],
                       now: datetime, top: int = 100) -> pd.DataFrame:
    """Persistent roster: every geolocated registro becomes a row tagged
    `visitado` (already has an F3 in integracion_f3) or `pendiente`. Pendientes
    are scored and only the top-N ranked ones are kept; visitados are all kept
    so the dashboard shows visited-vs-pending progress. `done` = the F3 set."""
    if df_integrada.empty or "registro_id" not in df_integrada.columns:
        return pd.DataFrame(columns=OUT_COLS + WEB_EXTRA_COLS)
    src_df = df_integrada.copy()

    # coords string column: the newer pipeline emits a single `coords`, the
    # older one `coords_unificadas`. Prefer whichever the sheet actually has.
    coords_col = next((c for c in ("coords_unificadas", "coords")
                       if c in src_df.columns), None)

    # lat/lon: explicit columns when present, else parsed from the coords string
    # (Cali-bbox guarded). reindex keeps a Series even if a column is absent, so
    # a schema that drops lat/lon degrades to the coords fallback, not a crash.
    lat = pd.to_numeric(src_df.reindex(columns=["lat"])["lat"], errors="coerce")
    lon = pd.to_numeric(src_df.reindex(columns=["lon"])["lon"], errors="coerce")
    if coords_col is not None:
        need = lat.isna() | lon.isna()
        parsed = src_df.loc[need, coords_col].map(parse_latlon)
        lat.loc[need] = parsed.map(lambda t: t[0] if t else float("nan"))
        lon.loc[need] = parsed.map(lambda t: t[1] if t else float("nan"))

    geoms = [shape(f["geometry"]) for f in zones]
    props = [f["properties"] for f in zones]
    tree = STRtree(geoms) if geoms else None

    def zone_for(x, y):
        if tree is None or pd.isna(x) or pd.isna(y):
            return None
        p = Point(x, y)
        for idx in tree.query(p, predicate="intersects"):
            if geoms[idx].covers(p):
                return props[idx]
        return None

    rows = []
    for i, (_, src) in enumerate(src_df.iterrows()):
        x, y = lon.iloc[i], lat.iloc[i]
        if pd.isna(x) or pd.isna(y):
            continue  # sin coordenadas no se puede despachar una cuadrilla
        zone = zone_for(x, y)
        ts = ts_by_visita.get(str(src.get("visita_id", "")).strip())
        age_days = ((now - ts).total_seconds() / 86400.0) if ts is not None else None

        flags = []
        if zone is None:
            flags.append("fuera_de_zona")
        if age_days is None:
            flags.append("sin_timestamp")

        fall = _num(src.get("n_fallecidos_total", ""))
        atrap = _num(src.get("n_atrapamientos_total", ""))
        resc = _num(src.get("n_rescatados_total", ""))
        gsev = graph_severity(" ".join(str(src.get(c, "")) for c in DAMAGE_TEXT_COLS))
        visitado = str(src.get("registro_id", "")).strip() in done
        rows.append({
            "estado_visita": "visitado" if visitado else "pendiente",
            "id_asignacion": id_asignacion(src.get("registro_id", "")),
            "registro_id": src.get("registro_id", ""),
            "direccion": src.get("direccion_unificada", ""),
            "comuna_corregimiento": normalize_comuna(src.get("comuna_unificada", "")),
            "barrio_vereda": src.get("barrio_unificado", ""),
            "coords": src.get(coords_col, "") if coords_col else "",
            "lat": round(float(y), 6),
            "lon": round(float(x), 6),
            "zona_id": zone["zone_id"] if zone else "",
            "ola": zone["ola"] if zone else "",
            "despacho": zone["despacho"] if zone else "",
            "nivel_riesgo": src.get("nivel_riesgo", ""),
            "estado_estructura": src.get("estado_estructura", ""),
            "grafo_severidad": round(gsev, 2),
            "n_fallecidos": int(fall),
            "n_atrapamientos": int(atrap),
            "n_rescatados": int(resc),
            "requiere_demolicion": src.get("requiere_demolicion", ""),
            "antiguedad_dias": round(age_days, 1) if age_days is not None else "",
            "timestamp_registro": ts.strftime("%Y-%m-%d %H:%M") if ts is not None else "",
            "flags": ",".join(flags),
            "_age": age_days,
            "_gsev": gsev,
            "_vfac": victim_factor(fall, atrap, resc),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=OUT_COLS + WEB_EXTRA_COLS)

    # Age is min-max normalized across pending points (the ones being ranked).
    ages = out.loc[out["estado_visita"] == "pendiente", "_age"].dropna()
    lo, span = (ages.min(), ages.max() - ages.min()) if len(ages) else (0.0, 0.0)

    def score(r) -> float:
        age_norm = ((r["_age"] - lo) / span) if (pd.notna(r["_age"]) and span > 0) else 0.0
        return round(
            WEIGHTS["grafo_severidad"] * r["_gsev"]
            + WEIGHTS["victimas"] * r["_vfac"]
            + WEIGHTS["antiguedad"] * age_norm
            + WEIGHTS["nivel_riesgo"] * riesgo_value(r["nivel_riesgo"])
            + WEIGHTS["requiere_demolicion"] * demolicion_value(r["requiere_demolicion"])
            + WEIGHTS["ola_zona"] * ola_value(r["ola"]), 1)

    out["score"] = out.apply(score, axis=1)
    out["_age_sort"] = out["_age"].fillna(-1.0)
    if out["id_asignacion"].duplicated().any():
        raise ValueError("id_asignacion collision detected; widen the id space.")

    pend = (out[out["estado_visita"] == "pendiente"]
            .sort_values(["score", "_age_sort", "registro_id"],
                         ascending=[False, False, True])
            .head(top).reset_index(drop=True))
    pend["prioridad"] = pend.index + 1
    vis = (out[out["estado_visita"] == "visitado"]
           .sort_values(["score", "registro_id"], ascending=[False, True])
           .reset_index(drop=True))
    vis["prioridad"] = ""   # visited points are done, not part of the worklist order

    roster = pd.concat([pend, vis], ignore_index=True)
    roster["fecha_corrida"] = now.strftime("%Y-%m-%d %H:%M")
    return roster[OUT_COLS + WEB_EXTRA_COLS]


# ── Self-check ────────────────────────────────────────────────────────────────
_KML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Folder><name>Zonas de asignación — Sismo Cali</name>
<Placemark><name>C03-Z02</name>
<description><![CDATA[OLA 1 · despacho #1 · Comuna 3<br>Barrios: San Juan Bosco; Santa Rosa<br>Carga 25.8 · 40.3 ha]]></description>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
-76.54,3.44,0 -76.53,3.44,0 -76.53,3.45,0 -76.54,3.45,0 -76.54,3.44,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>C19-Z01</name>
<description><![CDATA[OLA 2 · despacho #4 · Comuna 19]]></description>
<MultiGeometry>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
-76.56,3.40,0 -76.55,3.40,0 -76.55,3.41,0 -76.56,3.41,0 -76.56,3.40,0
</coordinates></LinearRing></outerBoundaryIs></Polygon>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
-76.58,3.42,0 -76.57,3.42,0 -76.57,3.43,0 -76.58,3.43,0 -76.58,3.42,0
</coordinates></LinearRing></outerBoundaryIs></Polygon>
</MultiGeometry></Placemark>
</Folder>
<Folder><name>Puntos de daño — Sismo Cali</name>
<Placemark><name>C01-Z01 · EDAN</name><Point><coordinates>-76.5,3.4,0</coordinates></Point></Placemark>
</Folder>
</Document></kml>"""


def _selfcheck():
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".kml", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(_KML_SAMPLE)
        tmp = Path(fh.name)
    zones = parse_zonas_kml(tmp)
    tmp.unlink()
    assert len(zones) == 2, zones  # point folder ignored
    z0 = zones[0]["properties"]
    assert z0 == {"zone_id": "C03-Z02", "ola": "1", "despacho": "1",
                  "comuna": "Comuna 3", "barrios": "San Juan Bosco; Santa Rosa",
                  "carga": "25.8"}, z0
    assert zones[0]["geometry"]["type"] == "Polygon"
    assert zones[1]["geometry"]["type"] == "MultiPolygon"

    # Graph severity: worst pathology's NSR-10/AIS peso wins; nothing -> 0.0.
    assert graph_severity("Aplastamiento del concreto en columna") == 0.9
    assert graph_severity("solo una fisura capilar") == 0.25
    assert graph_severity("edificación en buen estado") == 0.0
    assert graph_severity("") == 0.0
    # Comuna: bare number / any casing -> 'Comuna N'; corregimiento passes through.
    assert normalize_comuna("19") == "Comuna 19"
    assert normalize_comuna("comuna19") == "Comuna 19"
    assert normalize_comuna("COMUNA 5") == "Comuna 5"
    assert normalize_comuna("Comuna 3") == "Comuna 3"
    assert normalize_comuna("Corregimiento La Buitrera") == "Corregimiento La Buitrera"
    assert normalize_comuna("") == "" and normalize_comuna(None) == ""
    # Victims: any casualty > 0, fatalities weigh most, monotonic, saturating <1.
    assert victim_factor(0, 0, 0) == 0.0
    assert 0.0 < victim_factor(0, 0, 1) < victim_factor(0, 1, 0) < victim_factor(1, 0, 0) < 1.0
    assert victim_factor(2, 0, 0) > victim_factor(1, 0, 0)

    df_match = pd.DataFrame({
        "registro_id": ["AAAA-1111-XYZ12", "BBBB-2222", "-CCC33"],
        "match_method": ["handshake", "", "solo_f3"],
    })
    done = f3_done_registros(df_match)
    assert done == {"AAAA-1111"}, done

    now = datetime(2026, 8, 16, 12, 0)
    df_integrada = pd.DataFrame({
        "registro_id": ["AAAA-1111", "BBBB-2222", "CCCC-3333", "DDDD-4444"],
        "visita_id": ["1111", "2222", "3333", ""],
        "direccion_unificada": ["CL 1 # 2-3", "CL 4 # 5-6", "CL 7 # 8-9", "CL 10"],
        "barrio_unificado": ["B1", "B2", "B3", "B4"],
        "comuna_unificada": ["Comuna 3", "Comuna 3", "19", "Comuna 5"],  # "19" bare
        "lat": ["3.445", "3.445", "3.405", ""],
        "lon": ["-76.535", "-76.535", "-76.555", ""],
        "coords_unificadas": ["3.445, -76.535", "3.445, -76.535", "3.405, -76.555", ""],
        "nivel_riesgo": ["Alto", "Alto", "Bajo", ""],
        "estado_estructura": ["No Habitable", "No Habitable", "Habitable", ""],
        "descripcion_edan": ["daño", "Aplastamiento del concreto", "fisura leve", ""],
        "descripcion_visita": ["", "", "", ""],
        "nombre_estructura": ["", "", "", ""],
        "requiere_demolicion": ["Sí", "Sí", "No", ""],
        "n_fallecidos_total": ["0", "1", "0", ""],
        "n_atrapamientos_total": ["0", "0", "0", ""],
        "n_rescatados_total": ["0", "0", "0", ""],
    })
    ts = {"2222": pd.Timestamp(2026, 8, 11, 8, 0), "3333": pd.Timestamp(2026, 8, 15, 8, 0)}
    out = build_asignaciones(df_integrada, done, zones, ts, now, top=100)

    assert list(out.columns) == OUT_COLS + WEB_EXTRA_COLS
    assert out["registro_id"].is_unique
    assert "DDDD-4444" not in set(out["registro_id"])          # sin coords -> excluded
    assert len(out) == 3                                        # 2 pendientes + 1 visitado

    visita = dict(zip(out["registro_id"], out["estado_visita"]))
    assert visita == {"AAAA-1111": "visitado", "BBBB-2222": "pendiente",
                      "CCCC-3333": "pendiente"}, visita

    pend = out[out["estado_visita"] == "pendiente"].reset_index(drop=True)
    assert list(pend["registro_id"]) == ["BBBB-2222", "CCCC-3333"]  # score desc
    assert list(pend["prioridad"]) == [1, 2]
    assert (pend["score"].diff().dropna() <= 0).all()          # non-increasing
    r_top = pend.iloc[0]  # aplastamiento(0.9) + fatality + oldest + alto + demol + OLA1
    assert r_top["zona_id"] == "C03-Z02" and r_top["ola"] == "1"
    assert r_top["grafo_severidad"] == 0.9 and r_top["n_fallecidos"] == 1
    r_mid = pend[pend["registro_id"] == "CCCC-3333"].iloc[0]   # in multipolygon zone
    assert r_mid["zona_id"] == "C19-Z01" and r_mid["ola"] == "2"
    assert r_mid["grafo_severidad"] == 0.25
    assert r_mid["comuna_corregimiento"] == "Comuna 19"        # "19" bare -> normalized

    vis = out[out["estado_visita"] == "visitado"]
    assert (vis["prioridad"].astype(str) == "").all()          # visitados sin orden de worklist
    assert pd.notna(out["lat"]).all() and pd.notna(out["lon"]).all()
    assert out["id_asignacion"].str.fullmatch(r"[0-9A-Z]{5}").all()
    assert out["id_asignacion"].is_unique
    out2 = build_asignaciones(df_integrada, done, zones, ts, now, top=100)
    assert list(out2["id_asignacion"]) == list(out["id_asignacion"])  # stable across runs

    # Degraded inputs never crash: empty frames -> empty output
    assert f3_done_registros(pd.DataFrame()) == set()
    assert build_asignaciones(pd.DataFrame(), set(), zones, {}, now).empty
    # New pipeline schema: single `coords` column, no lat/lon -> parse fallback
    solo_coords = (df_integrada.drop(columns=["lat", "lon"])
                   .rename(columns={"coords_unificadas": "coords"}))
    out_fb = build_asignaciones(solo_coords, done, zones, ts, now)
    assert set(out_fb["registro_id"]) == set(out["registro_id"]), out_fb["registro_id"].tolist()
    assert (out_fb["coords"].astype(str).str.strip() != "").all()
    print("selfcheck ok")


# ── Main ──────────────────────────────────────────────────────────────────────
def load_visitas_timestamps(gc) -> dict[str, pd.Timestamp]:
    """visita_id -> parsed 'Marca temporal'. Empty dict (with warning) on failure."""
    try:
        vals = gc.open_by_key(VISITAS_SPREADSHEET_ID).worksheet(VISITAS_SHEET_NAME).get_all_values()
        header = vals[0]
        id_col = header.index("visita_id")
        ts_col = header.index("Marca temporal")
    except Exception as exc:  # noqa: BLE001 - resilience over precision here
        print(f"WARN: no pude leer timestamps de Visitas ({exc}); antiguedad=0 para todos")
        return {}
    out = {}
    for row in vals[1:]:
        vid = (row[id_col] if id_col < len(row) else "").strip()
        raw = row[ts_col] if ts_col < len(row) else ""
        ts = pd.to_datetime(raw, dayfirst=True, errors="coerce")
        if vid and pd.notna(ts):
            out[vid] = ts
    return out


def _read_tab(gc, spreadsheet_id, tab) -> pd.DataFrame:
    """Raw-text, row-aligned read (same contract as io_sheets._read_values)."""
    vals = gc.open_by_key(spreadsheet_id).worksheet(tab).get_all_values()
    if not vals:
        return pd.DataFrame()
    header, data = vals[0], vals[1:]
    width = len(header)
    data = [(r + [""] * width)[:width] for r in data]
    return pd.DataFrame(data, columns=header)


def export_web(out: pd.DataFrame, zones: list[dict], now: datetime) -> None:
    """Best-effort static artifacts for the dashboard: web/data/asignaciones.json
    (roster with lat/lon) + web/data/zonas_asignacion.geojson. Skipped silently
    when web/data is absent (e.g. running inside the integration container)."""
    web_data = REPO_ROOT / "web" / "data"
    if not web_data.exists():
        print(f"web/data ausente ({web_data}); no exporto al frontend")
        return
    records = out.where(out.notna(), None).to_dict(orient="records")
    n_vis = int((out["estado_visita"] == "visitado").sum())
    payload = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "pendientes": int(len(out) - n_vis),
        "visitados": n_vis,
        "zonas": len(zones),
        "records": records,
    }
    (web_data / "asignaciones.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    write_geojson(zones, web_data / "zonas_asignacion.geojson")
    # xlsx download for the dashboard button (sheet columns only, no lat/lon).
    try:
        out[OUT_COLS].to_excel(web_data / "asignaciones.xlsx", index=False)
        xlsx_note = " + asignaciones.xlsx"
    except Exception as exc:  # noqa: BLE001 - openpyxl may be absent; xlsx is optional
        xlsx_note = f" (xlsx no escrito: {exc})"
    print(f"web export: {len(records)} filas -> web/data/asignaciones.json "
          f"+ zonas_asignacion.geojson{xlsx_note}")


def main() -> dict:
    if "--check" in sys.argv:
        _selfcheck()
        return {}
    top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 100

    kml_path = resolve_kml()
    zones = parse_zonas_kml(kml_path)
    geojson_path = kml_path.parent / "zonas_asignacion.geojson"
    try:
        write_geojson(zones, geojson_path)
        print(f"{len(zones)} zonas desde {kml_path.name} -> {geojson_path.name}")
    except OSError as exc:  # geojson es un subproducto, no bloquear la corrida
        print(f"{len(zones)} zonas desde {kml_path.name} (geojson no escrito: {exc})")

    gc = gspread.authorize(credentials(READONLY))
    df_integrada = _read_tab(gc, EDAN_SPREADSHEET_ID, INTEGRADA_TAB)
    df_match = _read_tab(gc, F3_SPREADSHEET_ID, F3_MATCH_TAB)
    ts_by_visita = load_visitas_timestamps(gc)
    done = f3_done_registros(df_match)
    print(f"tabla_integrada: {len(df_integrada)} | con F3: {len(done)} "
          f"| timestamps visitas: {len(ts_by_visita)}")

    now = datetime.now()
    out = build_asignaciones(df_integrada, done, zones, ts_by_visita, now, top=top)
    n_vis = int((out["estado_visita"] == "visitado").sum()) if not out.empty else 0
    n_pend = len(out) - n_vis
    n_gsev = int((pd.to_numeric(out["grafo_severidad"], errors="coerce") > 0).sum()) if not out.empty else 0
    n_victimas = int(((out["n_fallecidos"].astype(int) + out["n_atrapamientos"].astype(int)
                       + out["n_rescatados"].astype(int)) > 0).sum()) if not out.empty else 0
    pend_scores = pd.to_numeric(out.loc[out["estado_visita"] == "pendiente", "score"], errors="coerce")
    score_max = float(pend_scores.max()) if len(pend_scores) else 0.0
    score_min = float(pend_scores.min()) if len(pend_scores) else 0.0
    print(f"asignaciones: {len(out)} filas | pendientes: {n_pend} | visitados: {n_vis} "
          f"| con daño en grafo: {n_gsev} | con víctimas: {n_victimas} "
          f"| score pendientes max/min: {score_max}/{score_min}")
    summary = {"filas": len(out), "pendientes": n_pend, "visitados": n_vis,
               "con_grafo": n_gsev, "con_victimas": n_victimas,
               "zonas_kml": len(zones), "registros_con_f3": len(done)}

    # Static artifacts for the dashboard (roster with lat/lon + zones geojson).
    export_web(out, zones, now)

    sheet_out = out[OUT_COLS]
    if "--dry" in sys.argv:
        path = "output/asignaciones.xlsx"
        sheet_out.to_excel(path, index=False)
        print(f"dry run: wrote {path} (no sheet write)")
        return summary

    ss = gspread.authorize(credentials(WRITE)).open_by_key(F3_SPREADSHEET_ID)
    try:
        dst = ss.worksheet(DST_TAB)
    except gspread.WorksheetNotFound:
        dst = ss.add_worksheet(DST_TAB, rows=len(sheet_out) + 10, cols=len(OUT_COLS))
    values = [OUT_COLS] + sheet_out.astype(str).where(sheet_out.notna(), "").values.tolist()
    dst.clear()
    dst.update(values=values, range_name="A1", value_input_option="RAW")
    print(f"wrote {len(sheet_out)} rows to {DST_TAB}")
    return summary


if __name__ == "__main__":
    main()
