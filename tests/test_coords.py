"""Coordinate parsing to WGS84 decimal: DMS, decimal (dot/comma), MAGNA-SIRGAS,
Google Maps URLs, and rejection of junk."""
import math

import pytest

from integracion.coords import coords_to_wgs84, parse_coords


def _approx(a, b, tol=1e-4):
    return abs(a - b) <= tol


def test_dms_to_decimal():
    lat, lon = parse_coords('3°27\'24.5"N 76°32\'35.2"W')
    assert _approx(lat, 3 + 27/60 + 24.5/3600)
    assert _approx(lon, -(76 + 32/60 + 35.2/3600))


def test_dms_oeste_letter_is_west():
    lat, lon = parse_coords('3°22\'22.02"N 76°33\'13"O')
    assert lat > 0 and lon < 0


def test_dms_labeled():
    res = parse_coords('LATITUD: 3°22\'20.73"N LONGITUD: 76°33\'17.80"W')
    assert res is not None and res[0] > 0 and res[1] < 0


def test_decimal_dot_and_comma():
    a = parse_coords("3.456789, -76.543210")
    b = parse_coords("3,4910915, -76,5191746")
    assert a and _approx(a[0], 3.456789)
    assert b and b[0] > 0 and b[1] < 0


def test_gmaps_url():
    res = parse_coords("https://maps.google.com/?q=3.4210,-76.5300")
    assert res and _approx(res[0], 3.4210)


def test_magna_sirgas_roundtrip():
    """A Cali WGS84 point → EPSG:3115 planar → back through the parser."""
    pyproj = pytest.importorskip("pyproj")
    fwd = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3115", always_xy=True)
    lon0, lat0 = -76.53, 3.44
    e, n = fwd.transform(lon0, lat0)
    res = parse_coords(f"{e:.2f}, {n:.2f}")
    assert res is not None
    assert _approx(res[0], lat0, tol=1e-3) and _approx(res[1], lon0, tol=1e-3)


def test_rejects_junk():
    for junk in ("", "-", "n/a", "sin dato", "no aplica", "hola mundo"):
        assert parse_coords(junk) is None


def test_coords_to_wgs84_format():
    s = coords_to_wgs84('3°27\'24.5"N 76°32\'35.2"W')
    assert s.count(",") == 1
    lat, lon = (float(x) for x in s.split(","))
    assert lat > 0 and lon < 0
