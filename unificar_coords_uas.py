"""One point per UAS flight place → `extraccions_coords_unificado` tab.

Reads the per-file `extraccion_coords` tab (written by extraer_coords_uas.py),
groups it by place — the first-level subfolder of each flight folder, whose name
is usually the inspected address — and resolves ONE coordinate per place:

1. The folder name is cleaned into an address candidate and geocoded through
   the Google Maps Geocoding API (integracion/geocode.py: cached, ROOFTOP /
   RANGE_INTERPOLATED only, pinned to Cali). Needs ``GOOGLE_MAPS_API_KEY`` in
   the environment or .env; without it the cascade simply skips to (2).
2. When the name does not look like an address, the geocoder rejects it, or the
   geocoded point lands > ``MAX_GEOCODE_DRIFT_M`` from where the drone actually
   took the photos (the address was "not right"), the centroid of the place's
   geotagged photos is used instead (median + outlier guard).

Both candidate coordinates plus their distance stay in the row for auditing.

    python unificar_coords_uas.py --dry-run   # print + output/extraccion_coords_unificado.xlsx
    python unificar_coords_uas.py             # also writes the tab
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter

import gspread
import pandas as pd

from integracion.coords import haversine_m
from integracion.exif_coords import aggregate_points
from integracion.gauth import credentials
from integracion.geocode import (API_KEY_ENV, GeocodeUnavailable, cache_key,
                                 geocode_one, load_cache, save_cache,
                                 to_google_address)

SPREADSHEET_ID = "1e4fj2LsnS00V_DkdsrZgBCzfQc-ZaEo0z3jp6rUC_vs"
SRC_TAB = "extraccion_coords"
DST_TAB = "extraccions_coords_unificado"
READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
WRITE = ["https://www.googleapis.com/auth/spreadsheets"]

# A geocode farther than this from the photo centroid means the folder name was
# misread as an address — the measured centroid wins.
MAX_GEOCODE_DRIFT_M = 500.0

OUT_COLS = ["carpeta_vuelo", "lugar", "lat", "lon", "fuente",
            "n_archivos", "n_fotos_gps", "dispersion_m",
            "lat_centroide", "lon_centroide", "lat_geocode", "lon_geocode",
            "distancia_geocode_centroide_m", "direccion_geocodificada"]

_ENUM_PREFIX = re.compile(r'^\s*\d+\s*[\).\.\-]\s*')          # "28. ", "2) "
_ORG_PREFIX = re.compile(r'^\s*(SIART|SGRED|SGC)\b\s*', re.IGNORECASE)
_PARENS = re.compile(r'\([^)]*\)')
_ROAD_WORD = re.compile(r'\b(CL|CLL|CALLE|KR|CRA|KRA|CR|CARRERA|AV|AVENIDA|DG|'
                        r'DIAGONAL|TV|TRANSVERSAL)\b', re.IGNORECASE)


def folder_to_address(name: str) -> str:
    """Address candidate from a place-folder name, '' when it is not one.

    "28. Calle 9 & Carrera 18" → "Calle 9 con Carrera 18"; "SGRED KR 56 CL 3" →
    "KR 56 CL 3"; "Fotogrametria" / "Centro (Aristi)" → "" (no road word + number
    → nothing worth paying a geocode call for). In the "1) L2 (Cl 3D KR 67)"
    naming the address lives INSIDE the parenthesis, so when the outer text is
    not an address each parenthetical is tried as one.
    """
    raw = str(name or "")
    for candidate in (_PARENS.sub(" ", raw), *re.findall(r'\(([^)]*)\)', raw)):
        s = _ORG_PREFIX.sub("", _ENUM_PREFIX.sub("", candidate))
        s = s.replace("&", " con ").replace("#", " # ")
        s = re.sub(r'\s+', ' ', s).strip(" ,.-")
        if _ROAD_WORD.search(s) and re.search(r'\d', s):
            return s
    return ""


def _read_tab(gc) -> pd.DataFrame:
    vals = gc.open_by_key(SPREADSHEET_ID).worksheet(SRC_TAB).get_all_values()
    header, data = vals[0], vals[1:]
    width = len(header)
    return pd.DataFrame([(r + [""] * width)[:width] for r in data], columns=header)


def _geocode_places(addresses: list[str]) -> dict[str, dict]:
    """``{address: record}`` through cache-then-API; silent no-op without a key."""
    cache = load_cache()
    pending = [a for a in addresses if cache_key(a) not in cache]
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if pending and not api_key:
        print(f"[unificar] {len(pending)} direcciones por geocodificar pero no hay "
              f"{API_KEY_ENV} — esos lugares usan el centroide de sus fotos")
    elif pending:
        import requests
        session, dirty = requests.Session(), False
        try:
            for addr in pending:
                try:
                    rec = geocode_one(to_google_address(addr), session, api_key)
                except GeocodeUnavailable as exc:
                    print(f"[unificar] geocoding no disponible: {exc}")
                    break
                rec["ts"], rec["direccion"] = int(time.time()), addr
                cache[cache_key(addr)] = rec
                dirty = True
                time.sleep(0.05)
        finally:
            if dirty:
                save_cache(cache)
    return {a: cache[cache_key(a)] for a in addresses if cache_key(a) in cache}


def _geocode_corner_batch(addresses: list[str]) -> dict[str, dict]:
    """Corner-level geocode (also accepts GEOMETRIC_CENTER) — last resort for
    places with neither a rooftop hit nor photos, where a calle×carrera corner
    is a fine pin for the flight map.

    NOT written to the shared cache on purpose: geocode.py rejects
    GEOMETRIC_CENTER because a corner poisons the EDAN matching tiers, and that
    cache is shared with the main pipeline — a corner cached as accepted would
    leak in. A handful of calls per run, so re-geocoding each run is cheap.
    """
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not addresses or not api_key:
        return {}
    import requests

    import integracion.geocode as g

    session = requests.Session()
    saved = g.GEOCODE_ACCEPTED
    g.GEOCODE_ACCEPTED = {**saved, "GEOMETRIC_CENTER": 150.0}
    out: dict[str, dict] = {}
    try:
        for addr in addresses:
            try:
                rec = geocode_one(to_google_address(addr), session, api_key)
            except GeocodeUnavailable as exc:
                print(f"[unificar] geocoding no disponible: {exc}")
                break
            except Exception:            # throttle/network — best effort, skip
                continue
            if rec.get("accepted"):
                out[addr] = rec
            time.sleep(0.05)
    finally:
        g.GEOCODE_ACCEPTED = saved
    if out:
        print(f"[unificar] {len(out)} lugar(es) rescatados con geocode de esquina "
              f"(aproximado, no cacheado)")
    return out


def build_unificado(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (carpeta_vuelo, first subfolder segment) with ONE coordinate.

    Cascade: precise geocode (rooftop/interpolated) → photo centroid → corner
    geocode (approximate) → nothing.
    """
    df = df.copy()
    df["lugar"] = df["subcarpeta"].str.split("/").str[0].replace("", "(raíz)")
    for col in ("lat", "lon"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Per place: its rows, EXIF-measured photo points (inherited video centroids
    # would double-count the same photos), centroid, and address candidate.
    places = {}
    for (vuelo, lugar), g in df.groupby(["carpeta_vuelo", "lugar"], sort=True):
        pts = list(zip(g.loc[g["fuente"] == "exif_drive", "lat"],
                       g.loc[g["fuente"] == "exif_drive", "lon"]))
        places[(vuelo, lugar)] = {"g": g, "pts": pts,
                                  "centroid": aggregate_points(pts),
                                  "addr": folder_to_address(lugar)}

    geocoded = _geocode_places(sorted({p["addr"] for p in places.values() if p["addr"]}))
    # Corner fallback only where there is an address, no precise geocode, no photos.
    corner = _geocode_corner_batch(sorted({
        p["addr"] for p in places.values()
        if p["addr"] and not p["centroid"]
        and not (geocoded.get(p["addr"]) or {}).get("accepted")}))

    rows = []
    for (vuelo, lugar), p in places.items():
        centroid, addr = p["centroid"], p["addr"]
        rec = geocoded.get(addr)
        geo = (rec["lat"], rec["lon"]) if rec and rec.get("accepted") else None
        crec = corner.get(addr)

        drift = round(haversine_m(geo, centroid[:2]), 1) if geo and centroid else None
        if geo and (drift is None or drift <= MAX_GEOCODE_DRIFT_M):
            lat, lon, fuente = geo[0], geo[1], "geocode"
        elif centroid:
            lat, lon, fuente = centroid[0], centroid[1], \
                "centroide_fotos" if not geo else "centroide_fotos_geocode_lejano"
        elif crec:
            lat, lon, fuente = crec["lat"], crec["lon"], "geocode_aproximado"
        else:
            lat, lon, fuente = None, None, "sin_coordenada"

        gsrc = rec if (rec and rec.get("accepted")) else crec   # geocode audit source
        rows.append({
            "carpeta_vuelo": vuelo, "lugar": lugar, "lat": lat, "lon": lon,
            "fuente": fuente, "n_archivos": len(p["g"]), "n_fotos_gps": len(p["pts"]),
            "dispersion_m": centroid[3] if centroid else None,
            "lat_centroide": centroid[0] if centroid else None,
            "lon_centroide": centroid[1] if centroid else None,
            "lat_geocode": gsrc["lat"] if gsrc else None,
            "lon_geocode": gsrc["lon"] if gsrc else None,
            "distancia_geocode_centroide_m": drift,
            "direccion_geocodificada": gsrc.get("formatted", "") if gsrc else "",
        })
    return pd.DataFrame(rows, columns=OUT_COLS)


def main() -> dict:
    gc = gspread.authorize(credentials(READONLY))
    df = _read_tab(gc)
    print(f"[unificar] {len(df)} filas leídas de {SRC_TAB}")

    out = build_unificado(df)
    print(f"[unificar] {len(out)} lugares | por fuente: {dict(Counter(out['fuente']))}")
    for _, r in out.iterrows():
        print(f"  {r['carpeta_vuelo']} / {r['lugar']}: {r['fuente']}"
              + (f" ({r['lat']:.5f}, {r['lon']:.5f})" if pd.notna(r["lat"]) else ""))
    summary = {"lugares": len(out), "fuentes": dict(Counter(out["fuente"]))}

    if "--dry-run" in sys.argv or "--dry" in sys.argv:
        path = "output/extraccion_coords_unificado.xlsx"
        out.to_excel(path, index=False)
        print(f"dry run: wrote {path} (no sheet write)")
        return summary

    ss = gspread.authorize(credentials(WRITE)).open_by_key(SPREADSHEET_ID)
    try:
        ws = ss.worksheet(DST_TAB)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(DST_TAB, rows=len(out) + 10, cols=len(OUT_COLS))
    values = [OUT_COLS] + out.astype(str).where(out.notna(), "").values.tolist()
    ws.clear()
    ws.update(values=values, range_name="A1", value_input_option="RAW")
    print(f"wrote {len(out)} rows to {DST_TAB}")
    return summary


if __name__ == "__main__":
    main()
