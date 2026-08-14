"""
Cadastral parcel downloader for the SPATIAL BRIDGE.

Pulls parcel (terreno/lote) polygons from an ArcGIS REST layer for ONLY the
bounding boxes that actually contain our points, asks the server to reproject
to WGS84 (outSR=4326), dedupes by parcel id, and writes a GeoJSON into
data/catastro/ where spatial_bridge.discover_local_parcels() picks it up.

Why per-bbox tiles: the target layers cap results (maxRecordCount ~2000) and
several do NOT support pagination, so a single city-wide query would silently
truncate. Tiling around our ~500 points keeps each query small and complete.

Reachability (checked 2026-08-14): Cali's own cadastre
(geoportal.cali.gov.co/agserver CATASTRO/Terrenos) was returning 502 and CVC's
geo.cvc.gov.co was DNS-blocked — likely quake-related outages. This module
changes nothing else: the moment either host answers, run `fetch_parcels()` and
the bridge switches from surrogate to parcel mode automatically.

Stdlib-only (urllib) — no extra dependency.
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

from .config import CATASTRO_DIR
from .coords import parse_latlon

# Candidate layers, best first. Cali-specific ones are the real prize; the IGAC
# national layer is a reachable fallback that (by design) excludes Cali but lets
# us validate the download plumbing end to end.
CANDIDATE_LAYERS = [
    # Cali municipal cadastre (ArcGIS MapServer) — "Terrenos" is typically layer 2.
    "https://geoportal.cali.gov.co/agserver/rest/services/CATASTRO/CATASTRO/MapServer/2",
    # CVC regional (needs a network that resolves geo.cvc.gov.co).
    "https://geo.cvc.gov.co/arcgis/rest/services/TERRITORIAL_ADMINISTRATIVA/Catastro_Alcaldia_Cali/MapServer/0",
    # IGAC national urban parcels (U_TERRENO) — reachable, excludes Cali.
    "https://mapas.igac.gov.co/server/rest/services/Dato_Fundamental_Catastro/MapServer/4",
]


def _get(url: str, timeout: int = 60) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "sismo-cali-integracion/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"[catastro] GET fail: {type(e).__name__}: {str(e)[:120]}")
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    if isinstance(data, dict) and data.get("error"):
        print(f"[catastro] server error: {str(data['error'])[:120]}")
        return None
    return data


def query_bbox(layer_url: str, bbox: tuple[float, float, float, float],
               out_fields: str = "*") -> list[dict]:
    """Query one ArcGIS layer over a WGS84 bbox (minlon,minlat,maxlon,maxlat).
    Returns GeoJSON features already in WGS84 (outSR=4326)."""
    params = {
        "where": "1=1",
        "geometry": ",".join(str(x) for x in bbox),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields, "returnGeometry": "true", "f": "geojson",
    }
    url = layer_url.rstrip("/") + "/query?" + urllib.parse.urlencode(params)
    data = _get(url, timeout=180)   # these cadastral servers can be very slow
    if not data:
        return []
    return data.get("features", []) or []


def _tiles_for_points(points: Iterable[tuple], pad_deg: float = 0.0025,
                      cell_deg: float = 0.01) -> list[tuple]:
    """Snap points to a coarse grid and return one padded bbox per occupied cell.
    ~0.01° ≈ 1.1 km cells keep each query well under the record cap."""
    cells = set()
    for p in points:
        if not p:
            continue
        lat, lon = p
        cells.add((math.floor(lat / cell_deg), math.floor(lon / cell_deg)))
    boxes = []
    for cy, cx in sorted(cells):
        lat0, lon0 = cy * cell_deg, cx * cell_deg
        boxes.append((lon0 - pad_deg, lat0 - pad_deg,
                      lon0 + cell_deg + pad_deg, lat0 + cell_deg + pad_deg))
    return boxes


def fetch_parcels(df_edan=None, df_visitas=None,
                  layers: Optional[list[str]] = None,
                  out_name: str = "cali_predios.geojson") -> Optional[str]:
    """Fetch parcels covering the coordinates present in EDAN + Visitas and save
    a GeoJSON. Returns the written path, or None if no layer answered.

    If df_* are None the sheets are loaded via the cache/pipeline loader.
    """
    if df_edan is None or df_visitas is None:
        from .pipeline import load_data
        df_edan, df_visitas = load_data(use_cache=True)

    points = [parse_latlon(x) for x in df_edan["coords"]] + \
             [parse_latlon(x) for x in df_visitas["coords"]]
    points = [p for p in points if p]
    if not points:
        print("[catastro] no hay coordenadas parseables — nada que descargar.")
        return None
    tiles = _tiles_for_points(points)
    print(f"[catastro] {len(points)} puntos → {len(tiles)} bboxes a consultar")

    for layer in (layers or CANDIDATE_LAYERS):
        print(f"[catastro] probando capa: {layer}")
        # cheap reachability probe
        meta = _get(layer.rstrip('/') + "?f=json", timeout=30)
        if not meta:
            continue
        feats: dict[str, dict] = {}
        empty_tiles = 0
        for i, bbox in enumerate(tiles):
            got = query_bbox(layer, bbox)
            if not got:
                empty_tiles += 1
                continue
            for f in got:
                props = f.get("properties", {}) or {}
                key = json.dumps(f.get("geometry"), sort_keys=True)[:200]
                feats[key] = f
        if feats:
            CATASTRO_DIR.mkdir(parents=True, exist_ok=True)
            out = CATASTRO_DIR / out_name
            fc = {"type": "FeatureCollection", "features": list(feats.values())}
            out.write_text(json.dumps(fc), encoding="utf-8")
            print(f"[catastro] OK: {len(feats)} predios → {out}")
            return str(out)
        print(f"[catastro] capa alcanzable pero 0 predios en el área "
              f"({empty_tiles}/{len(tiles)} bboxes vacíos) — probando siguiente.")
    print("[catastro] ninguna capa devolvió predios para el área de Cali.")
    return None


if __name__ == "__main__":
    fetch_parcels()
