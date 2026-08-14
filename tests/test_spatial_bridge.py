"""Spatial bridge: point-in-parcel assignment and the surrogate proximity link."""
import pandas as pd
import pytest

from integracion.spatial_bridge import add_spatial_bridge_matches, assign_parcels


def _square(lon0, lat0, half=0.00015, pid="LOTE-1"):
    """~30 m square parcel polygon as a (shapely_geom, id) tuple."""
    shapely = pytest.importorskip("shapely")
    from shapely.geometry import Polygon
    ring = [(lon0 - half, lat0 - half), (lon0 + half, lat0 - half),
            (lon0 + half, lat0 + half), (lon0 - half, lat0 + half)]
    return (Polygon(ring), pid)


def test_assign_parcels_point_in_polygon():
    parcel = _square(-76.53, 3.44)
    inside = (3.44, -76.53)          # (lat, lon)
    outside = (3.50, -76.60)
    ids = assign_parcels([inside, outside, None], [parcel])
    assert ids[0] == "LOTE-1"
    assert ids[1] is None and ids[2] is None


def _mini_frames():
    edan = pd.DataFrame({
        "sitio_id": ["S1", "S2"],
        "direccion_norm": ["CL 5 # 43-15, El Lido, Cali", "KR 9 # 1-2, Centro, Cali"],
        "barrio_vereda": ["El Lido", "Centro"],
        "coords": ["3.440000, -76.530000", "3.451000, -76.540000"],
    })
    visitas = pd.DataFrame({
        "visita_id": ["V1"],
        # different placa/text but ~12 m from S1, same barrio
        "direccion_norm": ["CL 5 # 43-99 APTO 3, El Lido, Cali"],
        "barrio_vereda": ["El Lido"],
        "coords": ["3.440100, -76.530050"],
    })
    return edan, visitas


def test_surrogate_bridge_links_nearby_same_barrio():
    edan, visitas = _mini_frames()
    mt = pd.DataFrame({"visita_id": ["V1"], "sitio_id": [None],
                       "match_method": [None], "match_score": [None]})
    out, info = add_spatial_bridge_matches(mt, edan, visitas, parcels=[])
    assert info["mode"] == "surrogate"
    row = out[out["visita_id"] == "V1"].iloc[0]
    assert row["sitio_id"] == "S1"
    assert row["match_method"] == "spatial_bridge"


def test_parcel_bridge_uses_shared_parcel():
    edan, visitas = _mini_frames()
    parcel = _square(-76.53, 3.44, pid="LOTE-A")   # contains both S1 and V1
    mt = pd.DataFrame({"visita_id": ["V1"], "sitio_id": [None],
                       "match_method": [None], "match_score": [None]})
    out, info = add_spatial_bridge_matches(mt, edan, visitas, parcels=[parcel])
    assert info["mode"] == "parcel"
    assert info["edan_with_parcel"] >= 1 and info["visitas_with_parcel"] == 1
    assert out[out["visita_id"] == "V1"].iloc[0]["sitio_id"] == "S1"
