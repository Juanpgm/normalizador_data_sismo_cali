"""
Harvest coordinates for Visitas from the EXIF GPS of the evidence photos linked
in `evidencia_soporte` (Google Drive links).

WHY THIS MATTERS: hundreds of visitas have NO parseable coordinate but DO have
photos. A camera's EXIF GPS is measured by instrument (metres), unlike a
hand-typed address — so any photo that kept its GPS gives a visita a
trustworthy coordinate, which directly feeds the geo / spatial-bridge / geo_key
tiers and can add SAFE matches.

HOW IT READS (measured on the real data, 2026-08-14):

* The whole uploads folder is listed with ``files.list`` — **6 requests, ~6 s**
  for 5.916 files — instead of one metadata GET per linked file (5.189
  requests). This is what makes the harvest cheap enough for the hourly job.
* Drive's ``imageMediaMetadata.location`` is faithful to the EXIF: downloading
  the bytes recovered GPS in 0 of 15 files where Drive reported none. So
  nothing is downloaded; ~15 GB of photo traffic is avoided.
* A visita links 5,8 GPS photos on average and they cluster on the site
  (dispersion to the centroid: 14 m median, 45 m p90). Averaging them is
  legitimate, with an outlier guard for the stray photo.

``harvest_from_folder`` remains as the offline path for when the images are
downloaded to data/evidencia/ instead.
"""
from __future__ import annotations

import io
import re
import statistics
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from .config import CALI_BBOX, DATA_DIR, DRIVE_READONLY_SCOPE, EXIF_OUTLIER_M
from .coords import haversine_m, parse_latlon

EVIDENCIA_DIR = DATA_DIR / "evidencia"
_ID_RE = re.compile(r'(?:id=|/d/|/file/d/)([A-Za-z0-9_-]{20,})')
_FILES_URL = "https://www.googleapis.com/drive/v3/files"
# Individual lookups only cover files the folder listing missed. The cap keeps a
# pathological case (the form spread uploads over many folders) from turning
# into thousands of requests inside the hourly job.
_STRAGGLER_LIMIT = 400


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


def _usable_point(loc) -> Optional[tuple[float, float]]:
    """Drive location dict → (lat, lon), rejecting the null island and non-Cali."""
    if not loc or loc.get("latitude") is None or loc.get("longitude") is None:
        return None
    lat, lon = float(loc["latitude"]), float(loc["longitude"])
    if lat == 0.0 and lon == 0.0:      # camera wrote an empty GPS block
        return None
    return (lat, lon) if _in_cali((lat, lon)) else None


# ── Aggregation: many photos of one visita → one coordinate ───────────────────
def aggregate_points(points, outlier_m: float = EXIF_OUTLIER_M):
    """Collapse a visita's photo GPS points into ONE mean coordinate.

    The centroid is taken as the component-wise MEDIAN first, because the mean
    is dragged by a single photo taken somewhere else and uploaded with the
    rest. Points farther than ``outlier_m`` from that median are dropped, and
    the survivors are averaged — that average is the reported coordinate.

    Returns ``(lat, lon, n_used, dispersion_m)`` or None when there is nothing
    usable. ``dispersion_m`` is the worst distance from a used point to the
    result, i.e. how tightly the photos agree.
    """
    pts = [(float(a), float(b)) for a, b in (points or [])]
    if not pts:
        return None
    med = (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))
    kept = [p for p in pts if haversine_m(p, med) <= outlier_m] or [med]
    lat = statistics.fmean(p[0] for p in kept)
    lon = statistics.fmean(p[1] for p in kept)
    dispersion = max((haversine_m(p, (lat, lon)) for p in kept), default=0.0)
    return (round(lat, 6), round(lon, 6), len(kept), round(dispersion, 1))


def _rows_from_points(points_by_visita: dict, source: str) -> pd.DataFrame:
    """Aggregated rows for every visita that ended up with at least one point."""
    rows = []
    for vid, pts in points_by_visita.items():
        agg = aggregate_points(pts)
        if agg:
            lat, lon, n_used, dispersion = agg
            rows.append({"visita_id": vid, "lat": lat, "lon": lon,
                         "n_fotos_gps": n_used, "dispersion_m": dispersion,
                         "source": source})
    return pd.DataFrame(rows, columns=["visita_id", "lat", "lon", "n_fotos_gps",
                                       "dispersion_m", "source"])


# ── Drive access ──────────────────────────────────────────────────────────────
def drive_session():
    """Authorized requests session for the read-only Drive API."""
    import requests
    from google.auth.transport.requests import Request

    from .gauth import credentials

    creds = credentials([DRIVE_READONLY_SCOPE])
    creds.refresh(Request())
    sess = requests.Session()
    sess.headers["Authorization"] = f"Bearer {creds.token}"
    return sess


def resolve_upload_folders(file_ids: Iterable[str], session, sample: int = 12) -> list[str]:
    """Parent folders of the evidence files, probed from a handful of samples.

    The form writes every upload into one "(File responses)" folder, but that is
    a convention and not a guarantee, so the parents of several files are read
    rather than assuming the first one covers everything.
    """
    folders: list[str] = []
    for fid in list(file_ids)[:sample]:
        resp = session.get(f"{_FILES_URL}/{fid}",
                           params={"fields": "parents", "supportsAllDrives": "true"},
                           timeout=30)
        if resp.status_code != 200:
            continue
        for parent in resp.json().get("parents", []) or []:
            if parent not in folders:
                folders.append(parent)
    return folders


def list_folder_gps(folder_id: str, session, seen: set | None = None
                    ) -> dict[str, tuple[float, float]]:
    """``{file_id: (lat, lon)}`` for every geotagged image in a Drive folder.

    One paginated ``files.list`` instead of one request per file: the whole
    uploads folder resolves in a handful of round trips.

    ``seen`` collects every id the listing covered, geotagged or not. Without
    it the caller cannot tell "this file was never listed" from "this file was
    listed and simply has no GPS", and would re-query thousands of files that
    were already answered — most photos carry no GPS at all.
    """
    out: dict[str, tuple[float, float]] = {}
    token = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "nextPageToken,files(id,imageMediaMetadata/location)",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if token:
            params["pageToken"] = token
        resp = session.get(_FILES_URL, params=params, timeout=60)
        if resp.status_code != 200:
            print(f"[exif] listado de {folder_id[:12]}… falló ({resp.status_code})")
            break
        payload = resp.json()
        for item in payload.get("files", []):
            if seen is not None:
                seen.add(item["id"])
            point = _usable_point((item.get("imageMediaMetadata") or {}).get("location"))
            if point:
                out[item["id"]] = point
        token = payload.get("nextPageToken")
        if not token:
            break
    return out


def _lookup_one(fid: str, session) -> Optional[tuple[float, float]]:
    """Single-file metadata fallback for ids the folder listing did not cover."""
    resp = session.get(f"{_FILES_URL}/{fid}",
                       params={"fields": "imageMediaMetadata(location)",
                               "supportsAllDrives": "true"}, timeout=30)
    if resp.status_code != 200:
        return None
    return _usable_point((resp.json().get("imageMediaMetadata") or {}).get("location"))


# ── Harvest: Drive ────────────────────────────────────────────────────────────
def harvest_via_drive(df_visitas, skip_visitas=frozenset(), session=None) -> pd.DataFrame:
    """Read the GPS of every evidence photo and aggregate it per visita.

    ``skip_visitas`` are left out entirely — visitas whose coordinate is already
    trustworthy (already harvested from EXIF, or backed by a pre-registration)
    must not be recomputed.
    """
    session = session or drive_session()

    wanted: dict[str, list[str]] = {}
    for vid, cell in zip(df_visitas["visita_id"], df_visitas.get("evidencia_soporte", [])):
        if vid in skip_visitas:
            continue
        ids = extract_file_ids(cell)
        if ids:
            wanted[vid] = ids
    if not wanted:
        print("[exif] no hay visitas por procesar (todas ya tienen coordenada confiable)")
        return _rows_from_points({}, "exif_drive")

    all_ids = [fid for ids in wanted.values() for fid in ids]
    gps: dict[str, tuple[float, float]] = {}
    covered: set[str] = set()
    folders = resolve_upload_folders(all_ids, session)
    for folder in folders:
        gps.update(list_folder_gps(folder, session, seen=covered))
    listed = len(gps)

    # Only files no listing ever covered are worth an individual request. A
    # listed file without GPS has already given its answer.
    missing = [fid for fid in dict.fromkeys(all_ids) if fid not in covered]
    for fid in missing[:_STRAGGLER_LIMIT]:
        point = _lookup_one(fid, session)
        if point:
            gps[fid] = point
    if len(missing) > _STRAGGLER_LIMIT:
        print(f"[exif] {len(missing) - _STRAGGLER_LIMIT} archivos fuera de las carpetas "
              f"listadas sin consultar (tope {_STRAGGLER_LIMIT})")
    elif missing:
        print(f"[exif] {len(missing)} archivos sueltos consultados uno a uno")

    points_by_visita = {vid: [gps[f] for f in ids if f in gps] for vid, ids in wanted.items()}
    df = _rows_from_points(points_by_visita, "exif_drive")
    print(f"[exif] {len(folders)} carpeta(s) · {listed} archivos con GPS listados · "
          f"{len(df)} visitas con coordenada de instrumento "
          f"(de {len(wanted)} con evidencia)")
    return df


# ── Harvest: local folder ─────────────────────────────────────────────────────
def harvest_from_folder(df_visitas, folder=EVIDENCIA_DIR) -> pd.DataFrame:
    """Scan a folder of downloaded evidence images and map EXIF GPS → visita_id.

    Images are matched to a visita by filename: either the Drive file id or the
    visita_id must appear in the file name.
    """
    folder = Path(folder)
    if not folder.exists():
        print(f"[exif] carpeta {folder} no existe — nada que procesar.")
        return _rows_from_points({}, "exif_local")
    fid2vid = build_fileid_to_visita(df_visitas)
    vids = set(df_visitas["visita_id"])
    points_by_visita: dict[str, list] = {}
    for img in folder.rglob("*"):
        if img.suffix.lower() not in {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff"}:
            continue
        latlon = exif_gps_from_bytes(img.read_bytes())
        if not latlon or not _in_cali(latlon):
            continue
        vid = next((fid2vid[f] for f in fid2vid if f in img.name), None) \
            or next((v for v in vids if v in img.stem), None)
        if vid:
            points_by_visita.setdefault(vid, []).append(latlon)
    df = _rows_from_points(points_by_visita, "exif_local")
    print(f"[exif] carpeta local: {len(df)} visitas con GPS de EXIF")
    return df


# ── Apply harvested coords into df_visitas ────────────────────────────────────
def apply_to_visitas(df_visitas, harvested: pd.DataFrame) -> pd.DataFrame:
    """Leave `coords` holding the most trustworthy value available.

    There is exactly ONE coordinate column. The EXIF average replaces a
    hand-typed coordinate (instrument beats typing), and `coords_fuente` records
    which one won so the choice stays auditable. Nothing is lost by overwriting:
    the source sheet is the record of what the volunteer typed and this pipeline
    never writes back to it.
    """
    df = df_visitas.copy()
    if "coords_fuente" not in df.columns:
        df["coords_fuente"] = ""
    if "coords_precision_m" not in df.columns:
        df["coords_precision_m"] = pd.NA

    coord_map, prec_map = {}, {}
    if harvested is not None and not harvested.empty:
        for _, r in harvested.iterrows():
            coord_map[r["visita_id"]] = f"{r['lat']:.6f}, {r['lon']:.6f}"
            prec_map[r["visita_id"]] = r["dispersion_m"]

    coords, fuentes, precisions = [], [], []
    replaced = filled = 0
    for vid, cur, fuente, prec in zip(df["visita_id"], df["coords"],
                                      df["coords_fuente"], df["coords_precision_m"]):
        if vid in coord_map:
            if parse_latlon(cur):
                replaced += 1
            else:
                filled += 1
            coords.append(coord_map[vid])
            fuentes.append("exif")
            precisions.append(prec_map[vid])
        else:
            coords.append(cur)
            fuentes.append(str(fuente) if str(fuente) == "exif"
                           else ("visita" if parse_latlon(cur) else ""))
            precisions.append(prec)
    df["coords"], df["coords_fuente"], df["coords_precision_m"] = coords, fuentes, precisions
    print(f"[exif] coords desde EXIF: {filled} rellenadas · {replaced} reemplazadas "
          f"(la escrita a mano cede ante la medida)")
    return df
