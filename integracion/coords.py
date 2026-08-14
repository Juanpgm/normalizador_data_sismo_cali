"""
Coordinate parsing → WGS84 decimal degrees.

Faithful port of EDA.ipynb section 3.3 plus the geo helpers of 3.4. Handles the
heterogeneous, hand-entered formats found in the source sheets:
  - Google Maps URLs (/@lat,lon · ?q=lat,lon · &ll=lat,lon)
  - DMS  (3°27'24.5"N 76°32'35.2"W · 3°22'13"O), optionally LATITUD/LONGITUD-labeled
  - decimal degrees, dot OR comma separator
  - MAGNA-SIRGAS / Colombia West planar (EPSG:3115) → WGS84 via pyproj

NOTE (volunteer data): coordinates are typed by hand, so they can be imprecise
or plainly wrong. Parsing only STANDARDIZES the value; downstream tiers treat
coordinates as corroborating evidence, never as ground truth.
"""
from __future__ import annotations

import math
import re
from typing import Optional, Tuple

# ── Empty / non-parseable sentinels ───────────────────────────────────────────
_COORD_EMPTY = frozenset({
    '', '-', ' ', 'n/a', 'na', 'nd', 's/d',
    'sin dato', 'ninguno', 'no tengo', 'sin coordenadas', 'no aplica',
})

# Unicode quote normalization: run FIRST so the DMS regex uses plain ' and ".
_QUOTE_NORM = str.maketrans({
    '‘': "'", '’': "'", 'ʼ': "'", '′': "'",
    '“': '"', '”': '"', 'ʺ': '"', '″': '"',
})

_GMAPS_AT = re.compile(r'@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)')
_GMAPS_Q = re.compile(r'[?&]q=(-?\d{1,3}\.\d+)[,+](-?\d{1,3}\.\d+)', re.IGNORECASE)
_GMAPS_LL = re.compile(r'[?&]ll=(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)', re.IGNORECASE)

_COORD_LABELS = re.compile(r'\b(?:LATITUD|LONGITUD|LAT|LON|LONG)\b\s*:?\s*', re.IGNORECASE)

_DMS = re.compile(
    r'(\d{1,3})\s*[°*]\s*(\d{1,2})\s*'
    "'"
    r'\s*(\d{1,2}(?:[.,]\d+)?)\s*'
    '"'
    r'{0,2}\s*([NSns])'
    r'[\s,;]+'
    r'(\d{1,3})\s*[°*]\s*(\d{1,2})\s*'
    "'"
    r'\s*(\d{1,2}(?:[.,]\d+)?)\s*'
    '"'
    r'{0,2}\s*([EWOewo])',
)

_DD = re.compile(r'(-?\d{1,3}\.\d+)\s*[,;\s]\s*(-?\d{1,3}\.\d+)')

_PLANAR_LABELED = re.compile(
    r'(?:[EN][:\s]*)(\d{6,7}(?:[.,]\d+)?)\s*[,;\s]\s*(?:[EN][:\s]*)(\d{6,7}(?:[.,]\d+)?)',
    re.IGNORECASE,
)
_PLANAR_BARE = re.compile(
    r'(?<!\d)(\d{6,7}(?:\.\d+)?)\s*[,;\s]\s*(\d{6,7}(?:\.\d+)?)(?!\d)'
)


def _normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s.translate(_QUOTE_NORM)).strip()


def _fix_decimal(s: str) -> float:
    return float(str(s).replace(',', '.'))


def _dms_to_dd(deg: str, mins: str, secs: str, hemi: str) -> float:
    dd = float(deg) + float(mins) / 60.0 + _fix_decimal(secs) / 3600.0
    return -dd if hemi.upper() in ('S', 'W', 'O') else dd


def _valid_wgs84(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def _plausible_cali(lat: float, lon: float) -> bool:
    return 2.0 <= lat <= 5.5 and -78.0 <= lon <= -75.0


def _try_gmaps_url(s: str) -> Optional[Tuple[float, float]]:
    for pat in (_GMAPS_AT, _GMAPS_Q, _GMAPS_LL):
        m = pat.search(s)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if _valid_wgs84(lat, lon):
                return lat, lon
    return None


def _try_dms(s: str) -> Optional[Tuple[float, float]]:
    clean = re.sub(r'\s+', ' ', _COORD_LABELS.sub(' ', s)).strip()
    m = _DMS.search(clean)
    if not m:
        return None
    lat = _dms_to_dd(m.group(1), m.group(2), m.group(3), m.group(4))
    lon = _dms_to_dd(m.group(5), m.group(6), m.group(7), m.group(8))
    return (lat, lon) if _valid_wgs84(lat, lon) else None


def _try_decimal(s: str) -> Optional[Tuple[float, float]]:
    m = _DD.search(s)
    if not m:
        m = _DD.search(re.sub(r'(\d),(\d)', r'\1.\2', s))
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    for lat, lon in ((a, b), (b, a)):
        if _valid_wgs84(lat, lon) and _plausible_cali(lat, lon):
            return lat, lon
    for lat, lon in ((a, b), (b, a)):
        if _valid_wgs84(lat, lon):
            return lat, lon
    return None


def _try_magna_sirgas(s: str) -> Optional[Tuple[float, float]]:
    """MAGNA-SIRGAS / Colombia West (EPSG:3115) → WGS84. No-op if pyproj absent."""
    try:
        from pyproj import Transformer
    except ImportError:
        return None
    m = _PLANAR_LABELED.search(s) or _PLANAR_BARE.search(s)
    if not m:
        return None
    e, n = _fix_decimal(m.group(1)), _fix_decimal(m.group(2))
    in_range_en = 900_000 <= e <= 1_300_000 and 700_000 <= n <= 1_200_000
    in_range_ne = 900_000 <= n <= 1_300_000 and 700_000 <= e <= 1_200_000
    if not (in_range_en or in_range_ne):
        return None
    if in_range_ne and not in_range_en:
        e, n = n, e
    try:
        t = Transformer.from_crs('EPSG:3115', 'EPSG:4326', always_xy=True)
        lon, lat = t.transform(e, n)
        return (lat, lon) if _valid_wgs84(lat, lon) else None
    except Exception:
        return None


def parse_coords(value) -> Optional[Tuple[float, float]]:
    """Parses a coordinate string → (lat, lon) WGS84 decimal degrees, or None."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    s = _normalize(raw)
    if not s or s.lower() in _COORD_EMPTY:
        return None
    return (
        _try_gmaps_url(s)
        or _try_dms(s)
        or _try_decimal(s)
        or _try_magna_sirgas(s)
    )


def coords_to_wgs84(value) -> str:
    """Returns 'lat, lon' in WGS84 decimal, or '' if not parseable."""
    result = parse_coords(value)
    if result is None:
        return ''
    lat, lon = result
    return f'{lat:.6f}, {lon:.6f}'


# ── Geo helpers (matching tiers) ──────────────────────────────────────────────
from .config import CALI_BBOX  # noqa: E402


def parse_latlon(val):
    """'3.4213, -76.5312' → (lat, lon) if inside the Cali bounding box, else None."""
    m = re.match(r'^\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*$', str(val))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    inside = (CALI_BBOX["lat_min"] <= lat <= CALI_BBOX["lat_max"]
              and CALI_BBOX["lon_min"] <= lon <= CALI_BBOX["lon_max"])
    return (lat, lon) if inside else None


def haversine_m(a, b) -> float:
    """Great-circle distance in meters between two (lat, lon) tuples."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))
