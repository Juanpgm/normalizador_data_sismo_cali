"""Prioritized F3 assignments from tabla_integrada + KML assignment zones.

Reads `tabla_integrada` (EDAN SISMO sheet) and the `integracion_f3` tab to find
registros that still have NO F3 inspection, assigns each pending point to an
assignment-zone polygon from the KML priorization map, scores every point with
a null-safe weighted sum (age of the original Google Forms record, risk level,
structure condition, demolition flag, victims, zone wave), and writes the top-N
ranked list to the `asignaciones` tab of the EDAN-F3 spreadsheet. It also
exports the KML zones as `basemaps/zonas_asignacion.geojson`.

Every scoring component degrades to 0 when its field is missing, so the run
never aborts on incomplete data. Points already matched in `integracion_f3`
are excluded, so successive runs walk through the remaining coverage.

Only the `asignaciones` tab is ever written. All other tabs are read-only.

    python asignar_f3.py --check     # offline self-check, no network
    python asignar_f3.py --dry       # real data, write output/asignaciones.xlsx only
    python asignar_f3.py             # real data, write the asignaciones tab
    python asignar_f3.py --top 150   # change the number of ranked rows (default 100)
"""
from __future__ import annotations

import hashlib
import json
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
from integracion.gauth import credentials

F3_SPREADSHEET_ID = "19k--nAEScol_3E7nbSpPev07gW2_UT8ojSsaMGbn6Ds"
INTEGRADA_TAB = "tabla_integrada"
F3_MATCH_TAB = "integracion_f3"
DST_TAB = "asignaciones"
READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
WRITE = ["https://www.googleapis.com/auth/spreadsheets"]

REPO_ROOT = Path(__file__).resolve().parents[1]
KML_PATH = REPO_ROOT / "basemaps" / "Mapa de Priorización 15_08_2026.kml"
GEOJSON_PATH = REPO_ROOT / "basemaps" / "zonas_asignacion.geojson"
KML_NS = "{http://www.opengis.net/kml/2.2}"
ZONES_FOLDER_HINT = "Zonas de asignaci"  # accent-safe prefix

# Tuning knob: component weights of the 0-100 priority score.
WEIGHTS = {
    "antiguedad": 35,          # days since the Google Forms timestamp, min-max normalized
    "nivel_riesgo": 25,        # Alto=1.0 Medio=0.6 Bajo=0.3
    "estado_estructura": 15,   # keyword severity of the reported condition
    "requiere_demolicion": 10,
    "victimas": 10,            # any fallecidos/atrapamientos reported
    "ola_zona": 5,             # KML zone wave: OLA 1=1.0 OLA 2=0.5
}

OUT_COLS = ["prioridad", "id_asignacion", "score", "registro_id", "direccion", "comuna", "barrio",
            "coords", "zona_id", "ola", "despacho", "nivel_riesgo",
            "estado_estructura", "requiere_demolicion", "antiguedad_dias",
            "timestamp_registro", "flags", "fecha_corrida"]


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


def estado_value(v) -> float:
    s = str(v or "").strip().lower()
    if not s or s in ("nan", "none", "-"):
        return 0.0
    if "colaps" in s:
        return 1.0
    if "riesgo" in s or "grave" in s:
        return 0.8
    if "afectad" in s or "dano" in s or "daño" in s:
        return 0.6
    return 0.4


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
    if df_integrada.empty or "registro_id" not in df_integrada.columns:
        return pd.DataFrame(columns=OUT_COLS)
    pending = df_integrada[~df_integrada["registro_id"].astype(str).isin(done)].copy()

    # reindex keeps this a Series (all-NaN) even if the sheet header drifts
    lat = pd.to_numeric(pending.reindex(columns=["lat"])["lat"], errors="coerce")
    lon = pd.to_numeric(pending.reindex(columns=["lon"])["lon"], errors="coerce")

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
    for i, (_, src) in enumerate(pending.iterrows()):
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

        victims = _num(src.get("n_fallecidos_total", "")) + _num(src.get("n_atrapamientos_total", ""))
        rows.append({
            "id_asignacion": id_asignacion(src.get("registro_id", "")),
            "registro_id": src.get("registro_id", ""),
            "direccion": src.get("direccion_unificada", ""),
            "comuna": src.get("comuna_unificada", ""),
            "barrio": src.get("barrio_unificado", ""),
            "coords": src.get("coords_unificadas", ""),
            "zona_id": zone["zone_id"] if zone else "",
            "ola": zone["ola"] if zone else "",
            "despacho": zone["despacho"] if zone else "",
            "nivel_riesgo": src.get("nivel_riesgo", ""),
            "estado_estructura": src.get("estado_estructura", ""),
            "requiere_demolicion": src.get("requiere_demolicion", ""),
            "antiguedad_dias": round(age_days, 1) if age_days is not None else "",
            "timestamp_registro": ts.strftime("%Y-%m-%d %H:%M") if ts is not None else "",
            "flags": ",".join(flags),
            "_age": age_days,
            "_victims": victims,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=OUT_COLS)

    ages = out["_age"].dropna()
    lo, span = (ages.min(), ages.max() - ages.min()) if len(ages) else (0.0, 0.0)

    def score(r) -> float:
        age_norm = ((r["_age"] - lo) / span) if (pd.notna(r["_age"]) and span > 0) else 0.0
        return round(
            WEIGHTS["antiguedad"] * age_norm
            + WEIGHTS["nivel_riesgo"] * riesgo_value(r["nivel_riesgo"])
            + WEIGHTS["estado_estructura"] * estado_value(r["estado_estructura"])
            + WEIGHTS["requiere_demolicion"] * demolicion_value(r["requiere_demolicion"])
            + WEIGHTS["victimas"] * (1.0 if r["_victims"] > 0 else 0.0)
            + WEIGHTS["ola_zona"] * ola_value(r["ola"]), 1)

    out["score"] = out.apply(score, axis=1)
    out["_age_sort"] = out["_age"].fillna(-1.0)
    out = (out.sort_values(["score", "_age_sort", "registro_id"],
                           ascending=[False, False, True])
              .head(top).reset_index(drop=True))
    out.insert(0, "prioridad", out.index + 1)
    out["fecha_corrida"] = now.strftime("%Y-%m-%d %H:%M")
    if out["id_asignacion"].duplicated().any():
        raise ValueError("id_asignacion collision detected; widen the id space.")
    return out[OUT_COLS]


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
        "comuna_unificada": ["Comuna 3", "Comuna 3", "Comuna 19", "Comuna 5"],
        "lat": ["3.445", "3.445", "3.405", ""],
        "lon": ["-76.535", "-76.535", "-76.555", ""],
        "coords_unificadas": ["3.445, -76.535", "3.445, -76.535", "3.405, -76.555", ""],
        "nivel_riesgo": ["Alto", "Alto", "Bajo", ""],
        "estado_estructura": ["Colapso Parcial", "Colapso Parcial", "", ""],
        "requiere_demolicion": ["Sí", "Sí", "No", ""],
        "n_fallecidos_total": ["0", "1", "0", ""],
        "n_atrapamientos_total": ["0", "0", "0", ""],
    })
    ts = {"2222": pd.Timestamp(2026, 8, 11, 8, 0), "3333": pd.Timestamp(2026, 8, 15, 8, 0)}
    out = build_asignaciones(df_integrada, done, zones, ts, now, top=100)

    assert "AAAA-1111" not in set(out["registro_id"])          # F3 done -> excluded
    assert list(out.columns) == OUT_COLS
    assert out["registro_id"].is_unique
    assert (out["score"].diff().dropna() <= 0).all()           # non-increasing
    r_top = out.iloc[0]  # oldest + alto + colapso + demolicion + victima + OLA1
    assert r_top["registro_id"] == "BBBB-2222" and r_top["zona_id"] == "C03-Z02"
    assert r_top["ola"] == "1" and r_top["score"] == 100.0
    r_mid = out[out["registro_id"] == "CCCC-3333"].iloc[0]     # in multipolygon zone
    assert r_mid["zona_id"] == "C19-Z01" and r_mid["ola"] == "2"
    assert "DDDD-4444" not in set(out["registro_id"])          # sin coords -> excluded
    assert len(out) == 2
    assert out["id_asignacion"].str.fullmatch(r"[0-9A-Z]{5}").all()
    assert out["id_asignacion"].is_unique
    out2 = build_asignaciones(df_integrada, done, zones, ts, now, top=100)
    assert list(out2["id_asignacion"]) == list(out["id_asignacion"])  # stable across runs

    # Degraded inputs never crash: empty frames and header drift -> empty output
    assert f3_done_registros(pd.DataFrame()) == set()
    assert build_asignaciones(pd.DataFrame(), set(), zones, {}, now).empty
    sin_coords_cols = df_integrada.drop(columns=["lat", "lon"])
    assert build_asignaciones(sin_coords_cols, done, zones, ts, now).empty
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


def main():
    if "--check" in sys.argv:
        _selfcheck()
        return
    top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 100

    zones = parse_zonas_kml(KML_PATH)
    write_geojson(zones, GEOJSON_PATH)
    print(f"{len(zones)} zonas KML -> {GEOJSON_PATH.name}")

    gc = gspread.authorize(credentials(READONLY))
    df_integrada = _read_tab(gc, EDAN_SPREADSHEET_ID, INTEGRADA_TAB)
    df_match = _read_tab(gc, F3_SPREADSHEET_ID, F3_MATCH_TAB)
    ts_by_visita = load_visitas_timestamps(gc)
    done = f3_done_registros(df_match)
    print(f"tabla_integrada: {len(df_integrada)} | con F3: {len(done)} "
          f"| timestamps visitas: {len(ts_by_visita)}")

    now = datetime.now()
    out = build_asignaciones(df_integrada, done, zones, ts_by_visita, now, top=top)
    n_zona = int(out["zona_id"].astype(str).str.strip().ne("").sum())
    n_ts = int(out["antiguedad_dias"].astype(str).str.strip().ne("").sum())
    print(f"asignaciones: {len(out)} puntos | con zona: {n_zona} | con antiguedad: {n_ts} "
          f"| score max/min: {out['score'].max()}/{out['score'].min()}")

    if "--dry" in sys.argv:
        path = "output/asignaciones.xlsx"
        out.to_excel(path, index=False)
        print(f"dry run: wrote {path} (no sheet write)")
        return

    ss = gspread.authorize(credentials(WRITE)).open_by_key(F3_SPREADSHEET_ID)
    try:
        dst = ss.worksheet(DST_TAB)
    except gspread.WorksheetNotFound:
        dst = ss.add_worksheet(DST_TAB, rows=top + 10, cols=len(OUT_COLS))
    values = [OUT_COLS] + out.astype(str).where(out.notna(), "").values.tolist()
    dst.clear()
    dst.update(values=values, range_name="A1", value_input_option="RAW")
    print(f"wrote {len(out)} rows to {DST_TAB}")


if __name__ == "__main__":
    main()
