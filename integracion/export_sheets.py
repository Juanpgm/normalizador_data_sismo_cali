"""
Publish the integration result to Google Sheets.

Sibling of ``pipeline.export_excel``: same inputs, different destination. The
hourly job uses this instead of producing an .xlsx nobody has to download.

This module writes into the LIVE source-of-truth document, so it is deliberately
paranoid. Its contract:

* only two worksheets are ever touched — ``tabla_integrada`` and
  ``integracion_stats``;
* worksheets are resolved by title AND their sheetId is verified against the
  pinned value in config, so a renamed or recreated tab aborts the run instead
  of overwriting a different tab;
* no worksheet is ever created, deleted, duplicated or renamed;
* the full value matrix is built in memory first and written in a single
  ``update`` per tab, padded with blanks to cover the previous extent. Nothing
  is cleared beforehand, so an interrupted run never leaves an empty tab.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import gspread
import pandas as pd

from . import __version__, metrics
from .config import (BLOCK_PLACA_TOL, EMBEDDING_MIN_SIM, FUZZY_MIN_LEN,
                     FUZZY_THRESHOLD, GEO_FAR_M, GEO_NEAR_M, METHOD_TRUST_BASE,
                     SPATIAL_BRIDGE_MAX_M, SPATIAL_CLUSTER_EPS_M, STATS_SHEET_ID,
                     STATS_SHEET_NAME, TABLA_SHEET_ID, TABLA_SHEET_NAME,
                     TARGET_SPREADSHEET_ID, TFIDF_MIN, TRUST_MIN, VECTOR_TOL,
                     WRITE_SCOPES)
from .gauth import credentials

BOGOTA = timezone(timedelta(hours=-5))
STATS_HEADER = ["seccion", "metrica", "valor"]
MAX_CELL_CHARS = 50_000      # hard Sheets limit per cell
MAX_REQUEST_CHARS = 1_500_000  # stay comfortably under the API payload cap


class SheetGuardError(RuntimeError):
    """A destination worksheet is not the one we pinned. Refuse to write."""


# ── Cell/value sanitization ───────────────────────────────────────────────────
def _cell(value):
    """Coerce any pandas/numpy value into something the Sheets API accepts.

    Missing values become the empty string; floats are rounded so coordinates
    do not arrive as 15-digit noise. Everything else is stringified, because
    the alternative (leaking a numpy scalar) fails JSON serialization.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int,)):
        return int(value)
    if isinstance(value, float):
        return round(float(value), 6)
    item = getattr(value, "item", None)
    if callable(item):          # numpy scalar
        return _cell(item())
    return str(value)[:MAX_CELL_CHARS]


def _pad(values: list[list], rows: int, cols: int) -> list[list]:
    """Grow the matrix to at least rows × cols with blanks.

    This is what lets us overwrite without clearing: the written range always
    covers whatever the tab held before, so stale rows are blanked in the same
    single request that writes the new ones.
    """
    width = max([cols] + [len(r) for r in values])
    out = [list(r) + [""] * (width - len(r)) for r in values]
    out.extend([[""] * width for _ in range(max(0, rows - len(out)))])
    return out


# ── Payload builders ──────────────────────────────────────────────────────────
def build_tabla_values(df_consolidada: pd.DataFrame) -> list[list]:
    """Header + rows for `tabla_integrada`, registro_id first."""
    df = df_consolidada.reset_index()
    header = [str(c) for c in df.columns]
    return [header] + [[_cell(v) for v in row] for row in df.itertuples(index=False)]


def _thresholds() -> dict:
    """The tunables that produced these numbers, read live from config so the
    published stats can never drift from the code that ran."""
    rows = {
        "vector_tol": VECTOR_TOL,
        "block_placa_tol": BLOCK_PLACA_TOL,
        "geo_near_m": GEO_NEAR_M,
        "geo_far_m": GEO_FAR_M,
        "tfidf_min": TFIDF_MIN,
        "fuzzy_threshold": FUZZY_THRESHOLD,
        "fuzzy_min_len": FUZZY_MIN_LEN,
        "embedding_min_sim": EMBEDDING_MIN_SIM,
        "spatial_cluster_eps_m": SPATIAL_CLUSTER_EPS_M,
        "spatial_bridge_max_m": SPATIAL_BRIDGE_MAX_M,
        "trust_min": TRUST_MIN,
    }
    rows.update({f"trust_base.{k}": v for k, v in METHOD_TRUST_BASE.items()})
    return rows


def build_stats_values(report: dict, match_table: pd.DataFrame | None = None,
                       run_info: dict | None = None) -> list[list]:
    """Header + `seccion | metrica | valor` rows for `integracion_stats`."""
    rows: list[list] = [STATS_HEADER]

    for section in ("matching", "integracion"):
        for key, value in metrics.flatten(report.get(section, {})).items():
            rows.append([section, key, _cell(value)])

    for key, value in _thresholds().items():
        rows.append(["umbrales", key, _cell(value)])

    if match_table is not None and "trust" in match_table.columns:
        accepted = match_table.loc[match_table["sitio_id"].notna(), "trust"]
        for method, group in match_table.loc[match_table["sitio_id"].notna()].groupby(
                "match_method")["trust"]:
            rows.append(["trust_por_metodo", f"{method}.n", int(len(group))])
            rows.append(["trust_por_metodo", f"{method}.media", _cell(group.mean())])
            rows.append(["trust_por_metodo", f"{method}.min", _cell(group.min())])
        if len(accepted):
            for label, lo, hi in (("0.70-0.79", 0.70, 0.80), ("0.80-0.89", 0.80, 0.90),
                                  ("0.90-0.94", 0.90, 0.95), ("0.95-1.00", 0.95, 1.01)):
                n = int(((accepted >= lo) & (accepted < hi)).sum())
                rows.append(["trust_distribucion", label, n])

    for key, value in (run_info or {}).items():
        rows.append(["ejecucion", key, _cell(value)])

    return rows


def run_info(started_at: datetime, *, with_embedding: bool,
             bridge_info: dict | None = None) -> dict:
    """Provenance block: when this ran, from where, and with which switches."""
    now = datetime.now(timezone.utc)
    return {
        "timestamp_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp_bogota": now.astimezone(BOGOTA).strftime("%Y-%m-%d %H:%M:%S"),
        "duracion_seg": round((now - started_at).total_seconds(), 1),
        "origen": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local",
        "git_sha": os.environ.get("GITHUB_SHA", "")[:8],
        "embedding_activo": with_embedding,
        "bridge_modo": (bridge_info or {}).get("mode", "n/a"),
        "version": __version__,
    }


# ── Writing ───────────────────────────────────────────────────────────────────
def _open_pinned(spreadsheet, title: str, expected_id: int):
    """Resolve a worksheet by title and refuse to proceed unless it is the exact
    tab we pinned. Never creates anything."""
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound as exc:
        raise SheetGuardError(
            f"Worksheet {title!r} does not exist in spreadsheet {spreadsheet.id}. "
            f"Create it manually — this job never creates worksheets."
        ) from exc
    if ws.id != expected_id:
        raise SheetGuardError(
            f"Worksheet {title!r} has sheetId {ws.id}, expected {expected_id}. "
            f"The tab was recreated or replaced; refusing to write. Update "
            f"config.py once you have confirmed the new tab is the right one."
        )
    return ws


def _chunks(values: list[list], max_chars: int = MAX_REQUEST_CHARS):
    """Split the matrix into row slices that stay under the API payload limit.

    A single update of ~2000 rows of free-text descriptions can exceed the
    Sheets request size cap, so large blocks go out as a few sequential
    top-down writes instead of one oversized request.
    """
    chunk, size, start = [], 0, 0
    for row in values:
        row_chars = sum(len(str(c)) for c in row) + len(row)
        if chunk and size + row_chars > max_chars:
            yield start, chunk
            start, chunk, size = start + len(chunk), [], 0
        chunk.append(row)
        size += row_chars
    if chunk:
        yield start, chunk


def _write(ws, values: list[list]) -> int:
    """Overwrite the worksheet with ``values``.

    Padded to the tab's current extent so stale rows are blanked by the same
    write — no ``clear()`` beforehand, so an interrupted run can never leave the
    tab empty. The grid is shrunk to the exact size afterwards to keep the next
    run's payload small.
    """
    needed_rows = len(values)
    padded = _pad(values, rows=ws.row_count, cols=ws.col_count)
    written_rows, written_cols = len(padded), len(padded[0])

    if ws.row_count < written_rows or ws.col_count < written_cols:
        ws.resize(rows=max(ws.row_count, written_rows),
                  cols=max(ws.col_count, written_cols))

    for offset, chunk in _chunks(padded):
        ws.update(values=chunk, range_name=f"A{offset + 1}",
                  value_input_option="RAW")

    # Trim the trailing blanks we just wrote, so the next run only has to cover
    # the real extent instead of re-blanking hundreds of empty rows every hour.
    if ws.row_count > needed_rows:
        ws.resize(rows=needed_rows, cols=ws.col_count)
    return needed_rows


def push_to_sheets(df_consolidada, match_table, report, *,
                   spreadsheet_id: str | None = None,
                   run_meta: dict | None = None,
                   client=None) -> dict:
    """Publish the integrated table and its statistics.

    Returns a summary dict with the row counts actually written. ``client`` is
    injectable so the tests can exercise the guards without touching the network.
    """
    spreadsheet_id = spreadsheet_id or TARGET_SPREADSHEET_ID
    gc = client or gspread.authorize(credentials(WRITE_SCOPES))
    sh = gc.open_by_key(spreadsheet_id)

    tabla_ws = _open_pinned(sh, TABLA_SHEET_NAME, TABLA_SHEET_ID)
    stats_ws = _open_pinned(sh, STATS_SHEET_NAME, STATS_SHEET_ID)

    tabla_values = build_tabla_values(df_consolidada)
    stats_values = build_stats_values(report, match_table, run_meta)

    tabla_rows = _write(tabla_ws, tabla_values)
    stats_rows = _write(stats_ws, stats_values)

    return {
        "spreadsheet_id": spreadsheet_id,
        TABLA_SHEET_NAME: {"filas": len(tabla_values) - 1,
                           "columnas": len(tabla_values[0]),
                           "rango_escrito": tabla_rows},
        STATS_SHEET_NAME: {"filas": len(stats_values) - 1,
                           "rango_escrito": stats_rows},
    }
