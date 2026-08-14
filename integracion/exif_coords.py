"""
Harvest coordinates for Visitas from the EXIF GPS of the evidence photos linked
in `evidencia_soporte` (Google Drive links).

WHY THIS MATTERS: 597 visitas have NO parseable coordinate but DO have a photo.
A camera's EXIF GPS is precise (metres), unlike a hand-typed address — so any
photo that kept its GPS gives a visita a trustworthy coordinate, which directly
feeds the geo / spatial-bridge tiers and can add SAFE matches.

ACCESS STATUS (checked 2026-08-14): the photos live in the FORM OWNER's Drive
and are NOT shared — the service account, the user's Drive and public download
all fail (404 / permission HTML). To unlock, EITHER:
  (A) the form owner shares the "…(File responses)" uploads folder with the
      service account email
        sismo-cali-reader@pmegeocode-1555299726775.iam.gserviceaccount.com
      (Viewer is enough) — then `harvest_via_drive()` reads
      imageMediaMetadata.location server-side (no download, cheap, batchable); OR
  (B) the owner downloads the uploads folder and drops the images into
      data/evidencia/ — then `harvest_from_folder()` reads EXIF GPS locally.

Either way, `apply_to_visitas()` fills the empty `coords` cells and the normal
pipeline picks the new coordinates up (geo + spatial_bridge tiers, trust geo
corroboration).
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import CALI_BBOX, DATA_DIR

EVIDENCIA_DIR = DATA_DIR / "evidencia"
_ID_RE = re.compile(r'(?:id=|/d/|/file/d/)([A-Za-z0-9_-]{20,})')
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


# ── Link parsing ──────────────────────────────────────────────────────────────
def extract_file_ids(cell) -> list[str]:
    """All Google Drive file IDs in an evidencia_soporte cell (comma-separated)."""
    s = str(cell)
    if not s or s.strip() in {"", "-", "nan", "None"}:
        return []
    return _ID_RE.findall(s)


def build_fileid_to_visita(df_visitas) -> dict[str, str]:
    """Map every evidence file id → its visita_id (first wins on collision)."""
    out = {}
    for vid, cell in zip(df_visitas["visita_id"], df_visitas.get("evidencia_soporte", [])):
        for fid in extract_file_ids(cell):
            out.setdefault(fid, vid)
    return out


# ── EXIF GPS → decimal (pure, unit-testable) ──────────────────────────────────
def _ratio(x) -> float:
    try:
        return float(x)
    except TypeError:
        return x[0] / x[1]


def gps_ifd_to_latlon(gps: dict) -> Optional[tuple[float, float]]:
    """Convert a PIL GPSInfo IFD dict → (lat, lon) WGS84 decimal, or None.

    Keys follow the EXIF GPS tag numbers: 1=LatRef 2=Lat 3=LonRef 4=Lon,
    each Lat/Lon being (deg, min, sec) rationals.
    """
    if not gps or 2 not in gps or 4 not in gps:
        return None
    def dms(v, ref):
        d, m, s = (_ratio(v[0]), _ratio(v[1]), _ratio(v[2]))
        dec = d + m / 60 + s / 3600
        return -dec if str(ref).upper() in ("S", "W") else dec
    try:
        lat = dms(gps[2], gps.get(1, "N"))
        lon = dms(gps[4], gps.get(3, "E"))
    except Exception:
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def exif_gps_from_bytes(data: bytes) -> Optional[tuple[float, float]]:
    """Read EXIF GPS from raw image bytes via PIL. None if absent/unreadable."""
    try:
        from PIL import Image, ExifTags
        img = Image.open(io.BytesIO(data))
        exif = img.getexif()
        if not exif:
            return None
        gps_tag = next((k for k, v in ExifTags.TAGS.items() if v == "GPSInfo"), 34853)
        gps = exif.get_ifd(gps_tag)
        return gps_ifd_to_latlon(dict(gps)) if gps else None
    except Exception:
        return None


def _in_cali(latlon) -> bool:
    lat, lon = latlon
    return (CALI_BBOX["lat_min"] <= lat <= CALI_BBOX["lat_max"]
            and CALI_BBOX["lon_min"] <= lon <= CALI_BBOX["lon_max"])


# ── Harvest: local folder ─────────────────────────────────────────────────────
def harvest_from_folder(df_visitas, folder=EVIDENCIA_DIR) -> pd.DataFrame:
    """Scan a folder of downloaded evidence images and map EXIF GPS → visita_id.

    Images are matched to a visita by filename: either the Drive file id or the
    visita_id must appear in the file name.
    """
    folder = Path(folder)
    rows = []
    if not folder.exists():
        print(f"[exif] carpeta {folder} no existe — nada que procesar.")
        return pd.DataFrame(columns=["visita_id", "lat", "lon", "source"])
    fid2vid = build_fileid_to_visita(df_visitas)
    vids = set(df_visitas["visita_id"])
    for img in folder.rglob("*"):
        if img.suffix.lower() not in {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff"}:
            continue
        latlon = exif_gps_from_bytes(img.read_bytes())
        if not latlon or not _in_cali(latlon):
            continue
        stem = img.stem
        vid = next((fid2vid[f] for f in fid2vid if f in img.name), None) \
            or next((v for v in vids if v in stem), None)
        if vid:
            rows.append({"visita_id": vid, "lat": round(latlon[0], 6),
                         "lon": round(latlon[1], 6), "source": "exif_local"})
    df = pd.DataFrame(rows).drop_duplicates("visita_id")
    print(f"[exif] carpeta local: {len(df)} visitas con GPS de EXIF")
    return df


# ── Harvest: Drive API (needs the uploads folder shared with the SA) ──────────
def harvest_via_drive(df_visitas, only_missing=True, limit=None) -> pd.DataFrame:
    """Read imageMediaMetadata.location for each evidence file via the Drive API
    using the service account. Requires the uploads folder to be shared with the
    SA. Falls back to downloading + local EXIF parse when Drive has no location.
    """
    import requests
    from google.auth.transport.requests import Request

    from .coords import parse_latlon
    from .gauth import credentials

    creds = credentials([_DRIVE_SCOPE])
    creds.refresh(Request())
    sess = requests.Session()
    sess.headers["Authorization"] = f"Bearer {creds.token}"

    targets = df_visitas
    if only_missing:
        mask = [not bool(parse_latlon(c)) for c in df_visitas["coords"]]
        targets = df_visitas[pd.Series(mask, index=df_visitas.index)]
    rows, tried, denied = [], 0, 0
    for _, r in targets.iterrows():
        got = None
        for fid in extract_file_ids(r.get("evidencia_soporte")):
            tried += 1
            resp = sess.get(f"https://www.googleapis.com/drive/v3/files/{fid}",
                            params={"fields": "imageMediaMetadata(location),mimeType",
                                    "supportsAllDrives": "true"}, timeout=30)
            if resp.status_code == 404:
                denied += 1
                continue
            if resp.status_code != 200:
                continue
            loc = (resp.json().get("imageMediaMetadata") or {}).get("location")
            if loc and "latitude" in loc and "longitude" in loc:
                ll = (loc["latitude"], loc["longitude"])
                if _in_cali(ll):
                    got = ll
                    break
            # fallback: download bytes and parse EXIF locally
            media = sess.get(f"https://www.googleapis.com/drive/v3/files/{fid}",
                             params={"alt": "media", "supportsAllDrives": "true"}, timeout=60)
            if media.status_code == 200:
                ll = exif_gps_from_bytes(media.content)
                if ll and _in_cali(ll):
                    got = ll
                    break
        if got:
            rows.append({"visita_id": r["visita_id"], "lat": round(got[0], 6),
                         "lon": round(got[1], 6), "source": "exif_drive"})
        if limit and len(rows) >= limit:
            break
    df = pd.DataFrame(rows)
    print(f"[exif] Drive API: {len(df)} visitas con GPS · {tried} archivos consultados"
          + (f" · {denied} sin acceso (compartir carpeta con el SA)" if denied else ""))
    return df


# ── Apply harvested coords into df_visitas (only where missing) ───────────────
def apply_to_visitas(df_visitas, harvested: pd.DataFrame) -> pd.DataFrame:
    """Fill empty `coords` cells from harvested EXIF coordinates. Non-destructive:
    existing coordinates are never overwritten. Returns a new DataFrame."""
    from .coords import parse_latlon
    if harvested is None or harvested.empty:
        return df_visitas
    coord_map = {r["visita_id"]: f"{r['lat']:.6f}, {r['lon']:.6f}"
                 for _, r in harvested.iterrows()}
    df = df_visitas.copy()
    filled = 0
    new = []
    for vid, cur in zip(df["visita_id"], df["coords"]):
        if not parse_latlon(cur) and vid in coord_map:
            new.append(coord_map[vid]); filled += 1
        else:
            new.append(cur)
    df["coords"] = new
    print(f"[exif] coords rellenadas desde EXIF: {filled}")
    return df
