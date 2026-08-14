"""
SPATIAL BRIDGE tier — the "puente espacial".

Idea: two records (one EDAN, one Visita) whose coordinates fall inside the SAME
cadastral parcel polygon describe the same site — even when their addresses are
written completely differently and their coordinates differ by a few meters.
The parcel boundary is a real cadastral unit, so it is stronger evidence than a
raw distance threshold.

CAUTION (volunteer data): coordinates are hand-entered and may be imprecise or
land in the wrong lot. Therefore this tier is used ONLY to link the two SOURCES
to each other (relative agreement), never to attach official cadastral
attributes as truth. Every candidate still passes a hard distance cap plus the
barrio and loose-numeric-coherence guards, and its trust is discounted for
coordinate uncertainty in trust.py.

Two modes, chosen automatically:
  1. parcel   — a cadastral polygon layer is available (local GeoJSON/shapefile
                or an ArcGIS point-query endpoint). Points are assigned to their
                containing parcel; matches share the parcel id.
  2. surrogate — no parcel layer reachable. Points are linked by proximity
                (BallTree haversine) within SPATIAL_CLUSTER_EPS_M, producing a
                shared spatial key. Clearly labelled and slightly lower trust.

No geopandas dependency: uses shapely (+ optional pyproj / fiona) directly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .config import (CATASTRO_DIR, SPATIAL_BRIDGE_MAX_M, SPATIAL_CLUSTER_EPS_M)
from .coords import haversine_m, parse_latlon
from .matching import barrio_ok, canonicalize_for_match, coherent
from .normalization import address_to_vector

_EARTH_R = 6_371_000.0  # meters


# ── Parcel layer loading ──────────────────────────────────────────────────────
def _id_from_props(props: dict) -> str:
    """Pick the most likely parcel identifier field from a feature's properties."""
    if not props:
        return ""
    prefer = ("NPN", "CODIGO_PREDIAL", "CODIGO_PRE", "CBML", "LOTE_CODIGO",
              "CODIGO", "COD_PREDIO", "PREDIO", "MANZANA_LO", "OBJECTID", "FID")
    upper = {k.upper(): v for k, v in props.items()}
    for key in prefer:
        if key in upper and str(upper[key]).strip():
            return str(upper[key]).strip()
    # fall back to the first non-empty scalar
    for v in props.values():
        if v not in (None, "") and not isinstance(v, (dict, list)):
            return str(v)
    return ""


def load_parcels_geojson(path) -> list[tuple]:
    """Load parcel polygons from a GeoJSON/JSON file → [(shapely_geom, id), ...].

    Assumes WGS84 (EPSG:4326). Silently returns [] if shapely is missing or the
    file cannot be parsed.
    """
    try:
        from shapely.geometry import shape
    except ImportError:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    feats = data.get("features", data if isinstance(data, list) else [])
    out = []
    for f in feats:
        geom = f.get("geometry") if isinstance(f, dict) else None
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        if g.is_empty or g.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        out.append((g, _id_from_props(f.get("properties", {}))))
    return out


def discover_local_parcels() -> list[tuple]:
    """Look for any parcel layer dropped into data/catastro/ (*.geojson, *.json)."""
    if not CATASTRO_DIR.exists():
        return []
    for pat in ("*.geojson", "*.json"):
        for f in sorted(CATASTRO_DIR.glob(pat)):
            parcels = load_parcels_geojson(f)
            if parcels:
                return parcels
    return []


def assign_parcels(points: list, parcels: list[tuple]) -> list[Optional[str]]:
    """Point-in-polygon: return the parcel id containing each (lat, lon), or None.

    Uses a shapely STRtree for speed. Points are (lat, lon); shapely uses (x=lon,
    y=lat).
    """
    if not parcels:
        return [None] * len(points)
    from shapely.geometry import Point
    from shapely import STRtree

    geoms = [g for g, _ in parcels]
    ids = [i for _, i in parcels]
    tree = STRtree(geoms)
    result: list[Optional[str]] = []
    for pt in points:
        if not pt:
            result.append(None)
            continue
        p = Point(pt[1], pt[0])  # (lon, lat)
        hit = None
        for idx in tree.query(p):
            if geoms[int(idx)].covers(p):
                hit = ids[int(idx)]
                break
        result.append(hit)
    return result


# ── The tier ──────────────────────────────────────────────────────────────────
def add_spatial_bridge_matches(match_table: pd.DataFrame, df_edan, df_visitas,
                               parcels: Optional[list[tuple]] = None,
                               max_m: float = SPATIAL_BRIDGE_MAX_M,
                               cluster_eps_m: float = SPATIAL_CLUSTER_EPS_M):
    """Tier: link still-unmatched visitas to an EDAN site via shared parcel /
    spatial proximity. Returns (updated_match_table, info_dict).

    info_dict carries the mode used and per-side parcel keys for auditing.
    """
    E = df_edan.reset_index(drop=True)
    V = df_visitas.reset_index(drop=True)
    e_geo = [parse_latlon(x) for x in E["coords"]]
    v_geo = [parse_latlon(x) for x in V["coords"]]
    e_sids = E["sitio_id"].to_numpy()
    e_barrio = E["barrio_vereda"].astype(str).to_numpy()
    v_barrio = V["barrio_vereda"].astype(str).to_numpy()
    e_vecs = np.array([address_to_vector(canonicalize_for_match(a))
                       for a in E["direccion_norm"]], dtype=np.float64)
    v_vecs = np.array([address_to_vector(canonicalize_for_match(a))
                       for a in V["direccion_norm"]], dtype=np.float64)

    if parcels is None:
        parcels = discover_local_parcels()
    mode = "parcel" if parcels else "surrogate"

    e_parcel = assign_parcels(e_geo, parcels) if parcels else [None] * len(E)
    v_parcel = assign_parcels(v_geo, parcels) if parcels else [None] * len(V)

    # EDAN index by parcel id (parcel mode)
    edan_by_parcel: dict[str, list[int]] = {}
    for i, pid in enumerate(e_parcel):
        if pid and e_geo[i]:
            edan_by_parcel.setdefault(pid, []).append(i)

    # For surrogate mode: EDAN points with coords, indexed for proximity search
    e_geo_idx = [i for i, g in enumerate(e_geo) if g]

    out = match_table.copy().set_index("visita_id")
    vid_pos = {v: j for j, v in enumerate(V["visita_id"])}
    added = 0
    limit_m = max_m if mode == "parcel" else cluster_eps_m

    for vid in list(out.index[out["sitio_id"].isna()]):
        j = vid_pos.get(vid)
        if j is None or not v_geo[j]:
            continue

        # candidate EDAN indices
        if mode == "parcel":
            pid = v_parcel[j]
            cands = edan_by_parcel.get(pid, []) if pid else []
        else:
            cands = e_geo_idx

        best = None  # (distance_m, edan_idx)
        for i in cands:
            if not e_geo[i]:
                continue
            dm = haversine_m(v_geo[j], e_geo[i])
            if dm > limit_m:
                continue
            if not barrio_ok(v_barrio[j], e_barrio[i]):
                continue
            if not coherent(v_vecs[j], e_vecs[i], strict=False):
                continue
            if best is None or dm < best[0]:
                best = (dm, i)

        if best is not None:
            i = best[1]
            out.loc[vid, ["sitio_id", "match_method", "match_score"]] = (
                e_sids[i], "spatial_bridge", round(best[0], 1))
            added += 1

    info = {
        "mode": mode,
        "n_parcels": len(parcels),
        "edan_with_parcel": sum(1 for x in e_parcel if x),
        "visitas_with_parcel": sum(1 for x in v_parcel if x),
        "added": added,
        "edan_parcel_key": dict(zip(e_sids, e_parcel)),
        "visita_parcel_key": dict(zip(V["visita_id"], v_parcel)),
    }
    print(f"spatial_bridge tier ({mode}): +{added} matches"
          + (f" · {info['n_parcels']} parcels" if parcels else ""))
    return out.reset_index(), info
