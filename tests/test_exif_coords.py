"""EXIF-coordinate harvesting: link parsing, GPS→decimal, aggregation, fill."""
import pandas as pd

from integracion.coords import haversine_m
from integracion.exif_coords import (aggregate_points, apply_to_visitas,
                                      build_fileid_to_visita, extract_file_ids,
                                      gps_ifd_to_latlon, list_folder_gps)


def test_extract_file_ids_multiple():
    cell = ("https://drive.google.com/open?id=1ENF1wplMbDYguzgsJ9ji4EhtfFAyUnGD, "
            "https://drive.google.com/open?id=12M1jK9kLE7Z8ka7QcU8I2jXjc7Gw-jj6")
    ids = extract_file_ids(cell)
    assert ids == ["1ENF1wplMbDYguzgsJ9ji4EhtfFAyUnGD", "12M1jK9kLE7Z8ka7QcU8I2jXjc7Gw-jj6"]


def test_extract_file_ids_empty():
    assert extract_file_ids("") == []
    assert extract_file_ids("nan") == []


def test_build_fileid_to_visita():
    df = pd.DataFrame({
        "visita_id": ["V1", "V2"],
        "evidencia_soporte": ["https://drive.google.com/open?id=AAAAAAAAAAAAAAAAAAAAAA",
                              "https://drive.google.com/open?id=BBBBBBBBBBBBBBBBBBBBBB"],
    })
    m = build_fileid_to_visita(df)
    assert m["AAAAAAAAAAAAAAAAAAAAAA"] == "V1"
    assert m["BBBBBBBBBBBBBBBBBBBBBB"] == "V2"


def test_gps_ifd_to_latlon_cali_point():
    # 3°27'24.5"N, 76°32'35.2"W  (a Cali coordinate)
    gps = {1: "N", 2: ((3, 1), (27, 1), (245, 10)),
           3: "W", 4: ((76, 1), (32, 1), (352, 10))}
    latlon = gps_ifd_to_latlon(gps)
    assert latlon is not None
    lat, lon = latlon
    assert abs(lat - (3 + 27/60 + 24.5/3600)) < 1e-4
    assert lon < 0  # West → negative
    assert abs(lon + (76 + 32/60 + 35.2/3600)) < 1e-4


def test_gps_ifd_missing_returns_none():
    assert gps_ifd_to_latlon({}) is None
    assert gps_ifd_to_latlon({1: "N"}) is None


# ── Aggregation: many photos → one coordinate ─────────────────────────────────
def test_aggregate_points_averages_a_tight_cluster():
    pts = [(3.44000, -76.53000), (3.44010, -76.53010), (3.43990, -76.52990)]
    lat, lon, n_used, dispersion = aggregate_points(pts)
    assert n_used == 3
    assert abs(lat - 3.44) < 1e-4 and abs(lon + 76.53) < 1e-4
    assert dispersion < 30


def test_aggregate_points_drops_the_stray_photo():
    """A photo taken elsewhere and uploaded with the rest must not drag the mean."""
    site = [(3.44000, -76.53000), (3.44010, -76.53010), (3.43995, -76.52995)]
    stray = (3.47000, -76.56000)          # ~4.5 km away
    lat, lon, n_used, _ = aggregate_points(site + [stray])
    assert n_used == 3                     # the stray was rejected
    assert haversine_m((lat, lon), site[0]) < 40


def test_aggregate_points_single_and_empty():
    assert aggregate_points([(3.44, -76.53)])[:2] == (3.44, -76.53)
    assert aggregate_points([(3.44, -76.53)])[3] == 0.0
    assert aggregate_points([]) is None
    assert aggregate_points(None) is None


# ── Bulk folder listing ───────────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, payload):
        self.status_code, self._payload = 200, payload

    def json(self):
        return self._payload


class _FakeSession:
    """Two pages of Drive results, so pagination is actually exercised."""

    def __init__(self, pages):
        self.pages, self.calls = pages, 0

    def get(self, url, params=None, timeout=None):
        page = self.pages[self.calls]
        self.calls += 1
        return _FakeResponse(page)


def test_list_folder_gps_paginates_and_filters():
    def f(fid, lat, lon):
        return {"id": fid, "imageMediaMetadata": {"location": {"latitude": lat,
                                                               "longitude": lon}}}
    session = _FakeSession([
        {"files": [f("a", 3.44, -76.53), f("b", 0.0, 0.0)], "nextPageToken": "t2"},
        {"files": [f("c", 40.7, -74.0),          # New York → outside Cali
                   f("d", 3.45, -76.54),
                   {"id": "e"}]},                # no image metadata at all
    ])
    gps = list_folder_gps("FOLDER", session)
    assert session.calls == 2                     # followed nextPageToken
    assert set(gps) == {"a", "d"}                 # null island and non-Cali dropped


# ── Applying the harvest ──────────────────────────────────────────────────────
def test_apply_to_visitas_measured_beats_typed():
    """Instrument GPS overwrites a hand-typed coordinate, and says so."""
    df = pd.DataFrame({
        "visita_id": ["V1", "V2", "V3"],
        "coords": ["", "3.400000, -76.500000", "3.410000, -76.510000"],
    })
    harvested = pd.DataFrame({
        "visita_id": ["V1", "V2"],
        "lat": [3.44, 3.99], "lon": [-76.53, -76.99],
        "n_fotos_gps": [3, 2], "dispersion_m": [12.0, 8.0],
        "source": ["exif_drive", "exif_drive"],
    })
    out = apply_to_visitas(df, harvested).set_index("visita_id")

    assert out.at["V1", "coords"] == "3.440000, -76.530000"   # filled a gap
    assert out.at["V2", "coords"] == "3.990000, -76.990000"   # replaced the typed one
    assert out.at["V3", "coords"] == "3.410000, -76.510000"   # untouched
    assert list(out["coords_fuente"]) == ["exif", "exif", "visita"]
    assert out.at["V1", "coords_precision_m"] == 12.0
    # exactly one coordinate column survives
    assert [c for c in out.columns if c.startswith("coords")] == \
        ["coords", "coords_fuente", "coords_precision_m"]


def test_apply_to_visitas_without_harvest_still_labels_source():
    df = pd.DataFrame({"visita_id": ["V1", "V2"],
                       "coords": ["3.400000, -76.500000", "no tengo"]})
    out = apply_to_visitas(df, pd.DataFrame()).set_index("visita_id")
    assert out.at["V1", "coords_fuente"] == "visita"
    assert out.at["V2", "coords_fuente"] == ""
