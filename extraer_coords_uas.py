"""Extract GPS coordinates from the UAS (drone) flight folders in Drive.

Walks the flight folder tree (top level = one folder per flight day, inside =
one subfolder per inspected address) and records one row per photo/video in the
`extraccion_coords` tab of the "Relación Vuelos UAS Sismo Cali" spreadsheet.

Where the coordinate comes from, in cascade:

* Photos: Drive's ``imageMediaMetadata.location`` — faithful to the EXIF and
  free (no download), same finding that powers integracion/exif_coords.py.
* Videos: Drive exposes no GPS for video, so
  1. a DJI ``.SRT`` sidecar with the same basename is downloaded (KBs) and its
     first GPS point parsed (``fuente=srt``),
  2. otherwise the video inherits the centroid of the geotagged photos in its
     own folder (``fuente=centroide_carpeta``),
  3. otherwise the row is kept with empty lat/lon (``fuente=sin_gps``).

Rows are never dropped for falling outside Cali — the ``fuera_cali`` column
flags them so the table stays auditable.

    python extraer_coords_uas.py --dry-run   # list + output/extraccion_coords.xlsx, no sheet write
    python extraer_coords_uas.py             # full run, writes the extraccion_coords tab
"""
from __future__ import annotations

import re
import sys
from collections import Counter

import gspread
import pandas as pd

from integracion.config import CALI_BBOX
from integracion.exif_coords import aggregate_points, drive_session
from integracion.gauth import credentials

ROOT_FOLDER_ID = "1IgA4dVPEn_Qz1DOxV4aG50AAaWJeBm-h"
DEST_SPREADSHEET_ID = "1e4fj2LsnS00V_DkdsrZgBCzfQc-ZaEo0z3jp6rUC_vs"
DST_TAB = "extraccion_coords"
SHEETS_WRITE = ["https://www.googleapis.com/auth/spreadsheets"]

_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_FOLDER_MIME = "application/vnd.google-apps.folder"

OUT_COLS = ["carpeta_vuelo", "subcarpeta", "archivo", "tipo", "lat", "lon",
            "alt_m", "fecha_captura", "fuente", "fuera_cali", "link", "file_id"]

# DJI SRT flavours seen in the wild:
#   [latitude: 3.441523] [longitude: -76.520432] [rel_alt: 42.1 abs_alt: 998.3]
#   GPS(-76.520432,3.441523,19)          <- (lon, lat, n_sats), Phantom style
_SRT_LABELED = re.compile(
    r"\[?\s*lat(?:itude)?\s*[:=]\s*(-?\d+\.\d+).*?\[?\s*long?(?:itude)?\s*[:=]\s*(-?\d+\.\d+)",
    re.IGNORECASE | re.DOTALL)
_SRT_GPS = re.compile(r"GPS\s*\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", re.IGNORECASE)
_SRT_ALT = re.compile(r"(?:rel_alt|altitude)\s*[:=]\s*(-?\d+\.?\d*)", re.IGNORECASE)


def parse_srt_gps(text: str):
    """First GPS point of a DJI subtitle file → (lat, lon, alt|None) or None."""
    lat = lon = None
    m = _SRT_LABELED.search(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
    else:
        m = _SRT_GPS.search(text)
        if m:
            # Phantom writes (lon, lat); swap when the first value cannot be a
            # latitude so both orderings are accepted.
            a, b = float(m.group(1)), float(m.group(2))
            lon, lat = (a, b) if abs(a) > 90 or abs(b) <= 90 else (b, a)
    if lat is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    alt = _SRT_ALT.search(text)
    return (lat, lon, float(alt.group(1)) if alt else None)


def _in_cali(lat, lon) -> bool:
    return (CALI_BBOX["lat_min"] <= lat <= CALI_BBOX["lat_max"]
            and CALI_BBOX["lon_min"] <= lon <= CALI_BBOX["lon_max"])


def _list_children(folder_id: str, session) -> list[dict]:
    """Every non-trashed child of a folder, paginated."""
    out, token = [], None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": ("nextPageToken,files(id,name,mimeType,createdTime,webViewLink,"
                       "imageMediaMetadata(location,time))"),
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if token:
            params["pageToken"] = token
        resp = session.get(_FILES_URL, params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        out.extend(payload.get("files", []))
        token = payload.get("nextPageToken")
        if not token:
            return out


def _download_text(file_id: str, session) -> str:
    resp = session.get(f"{_FILES_URL}/{file_id}",
                       params={"alt": "media", "supportsAllDrives": "true"}, timeout=60)
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")


def walk_tree(session, root_id: str = ROOT_FOLDER_ID) -> pd.DataFrame:
    """One row per photo/video under the flight tree, coordinates resolved."""
    rows, skipped = [], Counter()
    # (folder_id, carpeta_vuelo, subcarpeta) — top-level children of the root
    # become carpeta_vuelo; anything deeper accumulates into subcarpeta.
    stack = [(f["id"], f["name"], "")
             for f in _list_children(root_id, session) if f["mimeType"] == _FOLDER_MIME]
    while stack:
        folder_id, vuelo, sub = stack.pop()
        children = _list_children(folder_id, session)
        srt_by_stem = {c["name"].rsplit(".", 1)[0].lower(): c["id"]
                       for c in children if c["name"].lower().endswith(".srt")}
        photo_points = []      # geotagged photos of THIS folder, for the video fallback
        folder_rows = []
        for c in children:
            mime, name = c["mimeType"], c["name"]
            if mime == _FOLDER_MIME:
                stack.append((c["id"], vuelo, f"{sub}/{name}" if sub else name))
                continue
            meta = c.get("imageMediaMetadata") or {}
            if mime.startswith("image/"):
                tipo = "foto"
            elif mime.startswith("video/"):
                tipo = "video"
            else:
                if not name.lower().endswith(".srt"):   # sidecars are consumed, not rows
                    skipped[mime] += 1
                continue
            row = {"carpeta_vuelo": vuelo, "subcarpeta": sub, "archivo": name,
                   "tipo": tipo, "lat": None, "lon": None, "alt_m": None,
                   "fecha_captura": meta.get("time") or c.get("createdTime", ""),
                   "fuente": "sin_gps", "fuera_cali": "",
                   "link": c.get("webViewLink", ""), "file_id": c["id"]}
            loc = meta.get("location") or {}
            if tipo == "foto" and loc.get("latitude") is not None \
                    and (loc["latitude"], loc.get("longitude")) != (0.0, 0.0):
                row.update(lat=loc["latitude"], lon=loc["longitude"],
                           alt_m=loc.get("altitude"), fuente="exif_drive")
                photo_points.append((loc["latitude"], loc["longitude"]))
            elif tipo == "video":
                sidecar = srt_by_stem.get(name.rsplit(".", 1)[0].lower())
                if sidecar:
                    gps = parse_srt_gps(_download_text(sidecar, session))
                    if gps:
                        row.update(lat=gps[0], lon=gps[1], alt_m=gps[2], fuente="srt")
            folder_rows.append(row)

        centroid = aggregate_points(photo_points)
        for row in folder_rows:
            if row["fuente"] == "sin_gps" and row["tipo"] == "video" and centroid:
                row.update(lat=centroid[0], lon=centroid[1], fuente="centroide_carpeta")
            if row["lat"] is not None:
                row["fuera_cali"] = "" if _in_cali(row["lat"], row["lon"]) else "x"
        rows.extend(folder_rows)

    if skipped:
        print(f"[uas] archivos no foto/video omitidos: {dict(skipped)}")
    df = pd.DataFrame(rows, columns=OUT_COLS)
    df[["lat", "lon"]] = df[["lat", "lon"]].round(6)   # ~0.1 m, keeps the sheet legible
    df["alt_m"] = df["alt_m"].round(1)
    return df.sort_values(["carpeta_vuelo", "subcarpeta", "archivo"], ignore_index=True)


def main() -> dict:
    session = drive_session()
    df = walk_tree(session)

    con_gps = df["lat"].notna()
    print(f"[uas] {len(df)} archivos ({(df['tipo'] == 'foto').sum()} fotos, "
          f"{(df['tipo'] == 'video').sum()} videos) | con coordenada: {int(con_gps.sum())} "
          f"| por fuente: {dict(Counter(df['fuente']))}")
    for (vuelo, sub), g in df.groupby(["carpeta_vuelo", "subcarpeta"]):
        print(f"  {vuelo} / {sub or '.'}: {len(g)} archivos, {int(g['lat'].notna().sum())} con GPS")
    summary = {"archivos": len(df), "con_gps": int(con_gps.sum()),
               "fuentes": dict(Counter(df["fuente"]))}

    if "--dry-run" in sys.argv or "--dry" in sys.argv:
        path = "output/extraccion_coords.xlsx"
        df.to_excel(path, index=False)
        print(f"dry run: wrote {path} (no sheet write)")
        return summary

    ss = gspread.authorize(credentials(SHEETS_WRITE)).open_by_key(DEST_SPREADSHEET_ID)
    try:
        ws = ss.worksheet(DST_TAB)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(DST_TAB, rows=len(df) + 10, cols=len(OUT_COLS))
    values = [OUT_COLS] + df.astype(str).where(df.notna(), "").values.tolist()
    ws.clear()
    ws.update(values=values, range_name="A1", value_input_option="RAW")
    print(f"wrote {len(df)} rows to {DST_TAB}")
    return summary


if __name__ == "__main__":
    main()
