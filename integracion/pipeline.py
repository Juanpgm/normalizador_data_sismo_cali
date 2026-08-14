"""
End-to-end orchestration: load → standardize → match cascade → spatial bridge
→ LM tier → trust cutoff → integrate → metrics → Excel export.

Run from the CLI via run_integration.py, or import `run()` directly.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import pandas as pd

from . import integrate, metrics
from .config import OUTPUT_DIR
from .matching import build_match_table
from .spatial_bridge import add_spatial_bridge_matches, discover_local_parcels

_CACHE = OUTPUT_DIR / "cache"


# ── Data loading (with optional pickle cache) ─────────────────────────────────
def load_data(use_cache: bool = True):
    """Load + standardize EDAN and Visitas. Cached to avoid re-hitting Sheets."""
    ep, vp = _CACHE / "df_edan.pkl", _CACHE / "df_visitas.pkl"
    if use_cache and ep.exists() and vp.exists():
        with open(ep, "rb") as f:
            df_edan = pickle.load(f)
        with open(vp, "rb") as f:
            df_visitas = pickle.load(f)
        print(f"[cache] EDAN {df_edan.shape} · Visitas {df_visitas.shape}")
        return df_edan, df_visitas

    from .io_sheets import load_edan, load_visitas
    print("[sheets] leyendo EDAN y Visitas ...")
    df_edan, df_visitas = load_edan(), load_visitas()
    print(f"[sheets] EDAN {df_edan.shape} · Visitas {df_visitas.shape}")
    _CACHE.mkdir(parents=True, exist_ok=True)
    with open(ep, "wb") as f:
        pickle.dump(df_edan, f)
    with open(vp, "wb") as f:
        pickle.dump(df_visitas, f)
    return df_edan, df_visitas


# ── Full run ──────────────────────────────────────────────────────────────────
def run(use_cache: bool = True, with_embedding: bool = True,
        with_bridge: bool = True, export: bool = True) -> dict:
    df_edan, df_visitas = load_data(use_cache=use_cache)

    # Tiers 1-7
    match_table = build_match_table(df_edan, df_visitas)
    n1 = int(match_table["sitio_id"].notna().sum())
    print(f"cascada (t1-7): {n1} matches")

    # Spatial bridge tier
    bridge_info = None
    surrogate_vids = set()
    if with_bridge:
        parcels = discover_local_parcels()
        match_table, bridge_info = add_spatial_bridge_matches(
            match_table, df_edan, df_visitas, parcels=parcels)
        if bridge_info["mode"] == "surrogate":
            surrogate_vids = set(match_table.loc[
                match_table["match_method"] == "spatial_bridge", "visita_id"])

    # LM tier + trust need the embedding index (semantic verification)
    emb = None
    if with_embedding:
        try:
            from .embedding import EmbeddingIndex
            emb = EmbeddingIndex(df_edan, df_visitas)
            match_table = emb.add_embedding_matches(match_table)
        except Exception as e:  # download/runtime failure → degrade gracefully
            print(f"[warn] embedding tier unavailable: {type(e).__name__}: {e}")
            emb = None

    # Trust cutoff
    from .trust import compute_trust
    if emb is not None:
        match_table = compute_trust(match_table, emb, df_edan, df_visitas,
                                    surrogate_bridge_vids=surrogate_vids)
    else:
        match_table = _trust_without_embeddings(match_table, df_edan, df_visitas,
                                                surrogate_vids)

    n_final = int(match_table["sitio_id"].notna().sum())
    print(f"tras corte de confiabilidad: {n_final} matches")

    # Integration
    df_master = integrate.build_master(df_edan, match_table, df_visitas)
    df_integrado = integrate.build_integrado(df_master)
    df_confiable = integrate.build_confiable(df_integrado)
    df_consolidada = integrate.build_consolidada(df_integrado)

    report = metrics.build_report(match_table, df_edan, df_visitas, df_integrado, bridge_info)

    artifacts = {
        "df_edan": df_edan, "df_visitas": df_visitas, "match_table": match_table,
        "df_integrado": df_integrado, "df_confiable": df_confiable,
        "df_consolidada": df_consolidada, "report": report, "bridge_info": bridge_info,
    }
    if export:
        artifacts["excel_path"] = export_excel(df_consolidada, df_confiable,
                                               match_table, report)
        metrics.save_report(report, OUTPUT_DIR / "metricas.json")
    return artifacts


def _trust_without_embeddings(match_table, df_edan, df_visitas, surrogate_vids):
    """Trust fallback when the LM index is unavailable: base + barrio + geo only."""
    from rapidfuzz import fuzz

    from .config import METHOD_TRUST_BASE, TRUST_MIN
    from .coords import haversine_m, parse_latlon
    E, V = df_edan.reset_index(drop=True), df_visitas.reset_index(drop=True)
    e_pos = {s: i for i, s in enumerate(E["sitio_id"])}
    v_pos = {v: i for i, v in enumerate(V["visita_id"])}
    e_geo = [parse_latlon(x) for x in E["coords"]]
    v_geo = [parse_latlon(x) for x in V["coords"]]
    empt = {"", "-", "nan", "None"}

    def tr(vid, sid, method):
        j, i = v_pos[vid], e_pos[sid]
        t = METHOD_TRUST_BASE[method]
        b1, b2 = str(V["barrio_vereda"].iloc[j]).strip(), str(E["barrio_vereda"].iloc[i]).strip()
        if b1 not in empt and b2 not in empt:
            t += 0.03 if fuzz.token_sort_ratio(b1.upper(), b2.upper()) >= 75 else -0.08
        if v_geo[j] and e_geo[i]:
            dm = haversine_m(v_geo[j], e_geo[i])
            t += 0.05 if dm <= 60 else (-0.12 if dm > 250 else 0)
        if method == "spatial_bridge" and vid in surrogate_vids:
            t -= 0.10
        return round(min(max(t, 0.05), 0.99), 2)

    match_table = match_table.copy()
    match_table["trust"] = [tr(r["visita_id"], r["sitio_id"], r["match_method"])
                            if pd.notna(r["sitio_id"]) else None
                            for _, r in match_table.iterrows()]
    rej = match_table["trust"].notna() & (match_table["trust"] < TRUST_MIN)
    if rej.any():
        match_table.loc[rej, ["sitio_id", "match_method", "match_score", "trust"]] = \
            [None, None, None, None]
    return match_table


# ── Excel export ──────────────────────────────────────────────────────────────
def export_excel(df_consolidada, df_confiable, match_table, report,
                 path=None) -> str:
    import os

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = str(path or (OUTPUT_DIR / "integracion_consolidada.xlsx"))

    base, ext = os.path.splitext(path)
    for attempt in range(10):
        candidate = path if attempt == 0 else f"{base}_{attempt}{ext}"
        try:
            with open(candidate, "ab"):
                pass
            path = candidate
            break
        except PermissionError:
            continue

    # Metrics as a flat 2-column sheet
    flat = metrics.flatten(report)
    metricas_df = pd.DataFrame(flat.items(), columns=["metrica", "valor"])
    matches_only = match_table[match_table["sitio_id"].notna()].copy()

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df_consolidada.to_excel(writer, sheet_name="consolidada",
                                index=True, index_label="registro_id")
        df_confiable.to_excel(writer, sheet_name="confiable",
                              index=True, index_label="registro_id")
        matches_only[["visita_id", "sitio_id", "match_method", "match_score", "trust"]].to_excel(
            writer, sheet_name="matches", index=False)
        metricas_df.to_excel(writer, sheet_name="metricas", index=False)
        for name, ws in writer.sheets.items():
            ws.freeze_panes = "A2"
            for col_cells in ws.columns:
                width = max((len(str(c.value)) if c.value is not None else 0)
                            for c in col_cells)
                ws.column_dimensions[col_cells[0].column_letter].width = \
                    min(max(width + 2, 12), 55)

    print(f"Exportado: {path}")
    return path
