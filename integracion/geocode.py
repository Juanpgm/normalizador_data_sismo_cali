"""
Geocode addresses through the Google Maps Geocoding API — fill-only, cached,
precision-first.

WHY: EDAN carries a parseable coordinate for only ~9% of its sites, and the
geographic tiers (geo / spatial_bridge / geo_key) need BOTH ends of a pair.
Harvesting instrument GPS on the Visitas side barely moved the match rate for
exactly that reason. Geocoding the addresses is the lever that actually raises
the ceiling — and it also completes the deliverable's coordinate coverage.

Contract:

* Only rows whose `coords` is empty are ever touched. A measured (EXIF) or
  hand-typed coordinate is never overwritten by a geocoded one.
* Only ROOFTOP and RANGE_INTERPOLATED results are accepted, and the point must
  fall inside the Cali bounding box. GEOMETRIC_CENTER — the centre of a street
  or neighbourhood — is rejected: a coordinate hundreds of metres off poisons
  the geographic tiers.
* Every answer, including rejections, lands in a JSON cache keyed by the
  normalized address. Caching the rejections is what makes the hourly job
  idempotent: an address without a result is not re-paid every run.
* The cache is read from every known location (env override, the Railway
  volume, the repo) and merged newest-wins, so a cache built locally and
  committed seeds the Railway container and the API is only ever paid once
  per address.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import (CALI_BBOX, GEOCODE_ACCEPTED, GEOCODE_API_KEY_ENV,
                     GEOCODE_CACHE_ENV, GEOCODE_REPO_DIR, GEOCODE_URL,
                     GEOCODE_VOLUME_DIR)
from .coords import parse_latlon

API_KEY_ENV = GEOCODE_API_KEY_ENV
CACHE_ENV = GEOCODE_CACHE_ENV
CACHE_FILE = "geocode_cache.json"

_EMPTY = {"", "-", "nan", "None"}
_THROTTLE_S = 0.05             # ~20 req/s, well under Google's 50 QPS cap
_BACKOFFS_S = (2, 4, 8)        # OVER_QUERY_LIMIT retries


class GeocodeUnavailable(RuntimeError):
    """The API refused us outright (no key, no billing, key restricted)."""


class _Throttled(RuntimeError):
    """OVER_QUERY_LIMIT — retryable after a backoff."""


# ── Address translation ───────────────────────────────────────────────────────
# Inverse of matching._ROAD_WORDS: the matcher compresses road words to codes,
# the geocoder wants them spelled out. "CON" (corner) is left alone — Google
# Colombia understands it natively.
_ROAD_EXPANSIONS = [
    (re.compile(r'\b(?:KR|CRA|KRA|CR)\b\.?', re.IGNORECASE), 'Carrera'),
    (re.compile(r'\b(?:CL|CLL)\b\.?', re.IGNORECASE), 'Calle'),
    (re.compile(r'\bAV\b\.?', re.IGNORECASE), 'Avenida'),
    (re.compile(r'\bDG\b\.?', re.IGNORECASE), 'Diagonal'),
    (re.compile(r'\bTV\b\.?', re.IGNORECASE), 'Transversal'),
    (re.compile(r'\bNTE\b\.?', re.IGNORECASE), 'Norte'),
]
_TRAILING_CALI = re.compile(r',\s*Cali\s*$', re.IGNORECASE)


def to_google_address(direccion_norm) -> str:
    """Normalized address → the phrasing Google geocodes best.

    Unit noise (APTO/TORRE/…) is stripped from the STREET segment only — the
    same regex would eat "Urbanización Tequendama" if applied to the barrio.
    Road abbreviations are spelled out and the query is pinned to Cali.
    """
    from .matching import _UNIT_NOISE_RE

    s = str(direccion_norm or "").strip()
    if not s or s in _EMPTY:
        return ""
    street, _, rest = s.partition(",")
    street = _UNIT_NOISE_RE.sub(" ", street)
    for rx, word in _ROAD_EXPANSIONS:
        street = rx.sub(word, street)
    street = re.sub(r'\s+', ' ', street).strip(" ,")
    rest = _TRAILING_CALI.sub("", rest.strip(" ,")).strip(" ,")
    parts = [p for p in (street, rest) if p]
    return ", ".join(parts) + ", Cali, Valle del Cauca, Colombia"


def cache_key(direccion_norm) -> str:
    """Deterministic cache key for an address: case- and space-insensitive."""
    return re.sub(r'\s+', ' ', str(direccion_norm).strip()).upper()


# ── One request, validated ────────────────────────────────────────────────────
def _bounds_param() -> str:
    return (f"{CALI_BBOX['lat_min']},{CALI_BBOX['lon_min']}|"
            f"{CALI_BBOX['lat_max']},{CALI_BBOX['lon_max']}")


def geocode_one(address: str, session, api_key: str) -> dict:
    """Geocode a single address and validate the answer. Returns a cacheable
    record — accepted or rejected-with-reason. Raises GeocodeUnavailable when
    the API refuses us outright (that must stop the harvest, not be cached)."""
    resp = session.get(GEOCODE_URL, params={
        "address": address,
        "components": "country:CO|locality:Cali",
        "bounds": _bounds_param(),
        "region": "co",
        "language": "es",
        "key": api_key,
    }, timeout=30)
    payload = resp.json()
    status = payload.get("status", "")

    if status in ("REQUEST_DENIED", "INVALID_REQUEST"):
        raise GeocodeUnavailable(
            f"Geocoding API dijo {status}: {payload.get('error_message', '')}")
    if status == "OVER_QUERY_LIMIT":
        raise _Throttled(status)
    if status != "OK" or not payload.get("results"):
        return {"accepted": False, "reason": "sin_resultado"}

    result = payload["results"][0]
    loc_type = result["geometry"].get("location_type", "")
    if loc_type not in GEOCODE_ACCEPTED:
        return {"accepted": False, "reason": "precision_insuficiente",
                "location_type": loc_type}

    loc = result["geometry"]["location"]
    lat, lon = float(loc["lat"]), float(loc["lng"])
    if not (CALI_BBOX["lat_min"] <= lat <= CALI_BBOX["lat_max"]
            and CALI_BBOX["lon_min"] <= lon <= CALI_BBOX["lon_max"]):
        return {"accepted": False, "reason": "fuera_de_cali",
                "location_type": loc_type}

    return {
        "accepted": True,
        "lat": round(lat, 6), "lon": round(lon, 6),
        "location_type": loc_type,
        "precision_m": GEOCODE_ACCEPTED[loc_type],
        # In Cali the carrera/calle numbering is city-unique, so a partial
        # match caused by a misspelled barrio is usually still correct — but
        # it stays on the record for auditing.
        "partial": bool(result.get("partial_match", False)),
        "formatted": result.get("formatted_address", ""),
        "place_id": result.get("place_id", ""),
    }


# ── Cache ─────────────────────────────────────────────────────────────────────
def cache_dirs() -> list[Path]:
    """Every location a cache may live in, priority order. All are READ and
    merged; the first writable one receives the updates.

    ``GEOCODE_CACHE_DIR`` is a TOTAL override, not one more merge source: when
    set, only that directory exists. Anything else would let the real repo
    cache leak into tests and ops experiments.
    """
    override = os.environ.get(CACHE_ENV)
    if override:
        return [Path(override)]
    dirs = []
    for candidate in (GEOCODE_VOLUME_DIR, GEOCODE_REPO_DIR):
        # The Railway volume path is only meaningful on posix. On Windows it
        # would resolve to C:\data and _writable_dir would happily CREATE it.
        if candidate == GEOCODE_VOLUME_DIR and os.name != "posix":
            continue
        dirs.append(Path(candidate))
    return dirs


def load_cache(dirs=None) -> dict:
    """Merge every cache file found; on key collision the newest `ts` wins."""
    merged: dict[str, dict] = {}
    for d in dirs if dirs is not None else cache_dirs():
        path = Path(d) / CACHE_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for key, rec in data.items():
            if key not in merged or rec.get("ts", 0) > merged[key].get("ts", 0):
                merged[key] = rec
    return merged


def _writable_dir(dirs=None) -> Optional[Path]:
    for d in dirs if dirs is not None else cache_dirs():
        path = Path(d)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return path
        except (OSError, ValueError):
            continue
    return None


def save_cache(cache: dict, dirs=None) -> Optional[Path]:
    target = _writable_dir(dirs)
    if target is None:
        print("[geocode] sin directorio escribible para el cache — no se persiste")
        return None
    path = target / CACHE_FILE
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


# ── Harvest ───────────────────────────────────────────────────────────────────
def _targets(df) -> dict[str, str]:
    """``{cache_key: direccion_norm}`` for rows lacking a coordinate."""
    out: dict[str, str] = {}
    if df is None or "direccion_norm" not in getattr(df, "columns", []):
        return out
    for coords, addr in zip(df["coords"], df["direccion_norm"]):
        addr = str(addr).strip()
        if addr in _EMPTY or len(addr) < 7 or parse_latlon(coords):
            continue
        out.setdefault(cache_key(addr), addr)
    return out


def harvest_geocode(df_edan, df_visitas, *, session=None, api_key=None,
                    limit=None) -> tuple[dict, dict]:
    """Resolve every missing coordinate's address through cache-then-API.

    Returns ``(cache, info)``. The cache holds every known answer (old and
    new); `info` reports what this run actually did — most importantly
    ``llamadas_api``, which on a warm cache must be 0.
    """
    info = {"intentadas": 0, "cache_hits": 0, "llamadas_api": 0,
            "aceptadas_nuevas": 0, "rechazadas_nuevas": 0,
            "omitidas_por_limite": 0}
    cache = load_cache()

    wanted = {**_targets(df_edan), **_targets(df_visitas)}
    info["intentadas"] = len(wanted)
    pending = {k: a for k, a in wanted.items() if k not in cache}
    info["cache_hits"] = len(wanted) - len(pending)

    if not pending:
        print(f"[geocode] {info['cache_hits']} direcciones resueltas de cache · "
              f"0 llamadas a la API")
        return cache, info

    api_key = api_key or os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        print(f"[geocode] {len(pending)} direcciones por resolver pero no hay "
              f"{API_KEY_ENV} — se omite la geocodificación (ver plan de setup)")
        return cache, info

    if session is None:
        import requests
        session = requests.Session()

    dirty = False
    try:
        for key, addr in pending.items():
            if limit is not None and info["llamadas_api"] >= limit:
                info["omitidas_por_limite"] += 1
                continue
            record = None
            for backoff in (0, *_BACKOFFS_S):
                if backoff:
                    time.sleep(backoff)
                try:
                    record = geocode_one(to_google_address(addr), session, api_key)
                    break
                except _Throttled:
                    continue
            if record is None:          # throttled through every backoff
                continue
            info["llamadas_api"] += 1
            record["ts"] = int(time.time())
            record["direccion"] = addr
            cache[key] = record
            dirty = True
            info["aceptadas_nuevas" if record["accepted"] else
                 "rechazadas_nuevas"] += 1
            time.sleep(_THROTTLE_S)
    finally:
        if dirty:
            save_cache(cache)

    print(f"[geocode] {info['cache_hits']} de cache · {info['llamadas_api']} "
          f"llamadas a la API · +{info['aceptadas_nuevas']} aceptadas · "
          f"{info['rechazadas_nuevas']} rechazadas"
          + (f" · {info['omitidas_por_limite']} fuera del límite"
             if info["omitidas_por_limite"] else ""))
    return cache, info


# ── Apply (fill-only) ─────────────────────────────────────────────────────────
def apply_geocode(df, cache: dict, *, typed_label: str) -> pd.DataFrame:
    """Fill empty `coords` from accepted cache entries. Never overwrites.

    Also normalizes the provenance columns so both tables carry them: an
    existing parseable coordinate keeps (or gains) its label — ``exif`` and
    ``geocode`` are preserved, anything else typed becomes ``typed_label``.
    """
    df = df.copy()
    if "coords_fuente" not in df.columns:
        df["coords_fuente"] = ""
    if "coords_precision_m" not in df.columns:
        df["coords_precision_m"] = pd.NA

    coords, fuentes, precisions = [], [], []
    filled = 0
    for cur, fuente, prec, addr in zip(df["coords"], df["coords_fuente"],
                                       df["coords_precision_m"],
                                       df["direccion_norm"]):
        if parse_latlon(cur):
            coords.append(cur)
            fuentes.append(str(fuente) if str(fuente) in ("exif", "geocode")
                           else typed_label)
            precisions.append(prec)
            continue
        rec = cache.get(cache_key(addr))
        if rec and rec.get("accepted"):
            coords.append(f"{rec['lat']:.6f}, {rec['lon']:.6f}")
            fuentes.append("geocode")
            precisions.append(rec.get("precision_m"))
            filled += 1
        else:
            coords.append(cur)
            fuentes.append("")
            precisions.append(prec)
    df["coords"], df["coords_fuente"], df["coords_precision_m"] = \
        coords, fuentes, precisions
    print(f"[geocode] {typed_label}: {filled} coords rellenadas desde geocodificación")
    return df
