"""
Horizontal merge + final integrated dataset. Faithful port of EDA.ipynb
sections 6.5-6.8: outer merge (nothing is lost), source flag, per-record
trust_score, max-consolidated shared metrics, WGS84 coordinates, coalesced
text columns, and the delivery subsets.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .coords import parse_latlon
from .matching import address_to_vector, canonicalize_for_match

SHARED_NUMERIC = ["n_fallecidos", "n_atrapamientos", "n_rescatados"]

_REDUNDANT = [
    "direccion_norm_edan", "direccion_norm_visita",
    "barrio_vereda_edan", "barrio_vereda_visita",
    "comuna_corregimiento_edan", "comuna_corregimiento_visita",
    "coords_edan", "coords_visita",
    "n_fallecidos_edan", "n_fallecidos_visita",
    "n_atrapamientos_edan", "n_atrapamientos_visita",
    "n_rescatados_edan", "n_rescatados_visita",
    "integration_handshake_edan", "integration_handshake_visita",
    "match_score", "trust",
]


def build_master(df_edan, match_table, df_visitas) -> pd.DataFrame:
    """Outer merge EDAN ⨝ match_table ⨝ Visitas, keyed by a combined index."""
    df_master = (
        df_edan
        .merge(match_table, on="sitio_id", how="outer")
        .merge(df_visitas, on="visita_id", how="outer", suffixes=("_edan", "_visita"))
    )
    registro_id = df_master["sitio_id"].fillna("----") + "-" + df_master["visita_id"].fillna("----")
    df_master.insert(0, "registro_id", registro_id)
    return df_master.set_index("registro_id")


def build_integrado(df_master) -> pd.DataFrame:
    """Final integrated dataset with source flag, trust_score, unified columns."""
    df = df_master.copy()

    fuente = np.select(
        [df["sitio_id"].notna() & df["visita_id"].notna(), df["sitio_id"].notna()],
        ["edan+visita", "solo_edan"], default="solo_visita")
    df.insert(0, "fuente", fuente)

    def _coalesce(col_edan, col_visita):
        a = df[col_edan].astype("object")
        b = df[col_visita].astype("object")
        a_empty = a.isna() | a.astype(str).str.strip().isin(["", "-", "nan", "None"])
        return a.mask(a_empty, b)

    for pos, (name, ce, cv) in enumerate([
        ("direccion_unificada", "direccion_norm_edan", "direccion_norm_visita"),
        ("barrio_unificado", "barrio_vereda_edan", "barrio_vereda_visita"),
        ("comuna_unificada", "comuna_corregimiento_edan", "comuna_corregimiento_visita"),
    ], start=1):
        if ce in df.columns and cv in df.columns:
            df.insert(pos, name, _coalesce(ce, cv))

    # Coordinates → WGS84 decimal
    _pts_e = df["coords_edan"].map(parse_latlon) if "coords_edan" in df else pd.Series([None] * len(df), index=df.index)
    _pts_v = df["coords_visita"].map(parse_latlon) if "coords_visita" in df else pd.Series([None] * len(df), index=df.index)
    _pts = _pts_e.where(_pts_e.notna(), _pts_v)
    df.insert(4, "lat", _pts.map(lambda p: round(p[0], 6) if p else np.nan))
    df.insert(5, "lon", _pts.map(lambda p: round(p[1], 6) if p else np.nan))
    df.insert(6, "coords_unificadas",
              _pts.map(lambda p: f"{p[0]:.6f}, {p[1]:.6f}" if p else None))

    def _as_num(col):
        s = df[col].astype(str).str.strip().str.replace(",", "", regex=False)
        return pd.to_numeric(s.str.extract(r"(-?\d+\.?\d*)", expand=False), errors="coerce")

    for name in SHARED_NUMERIC:
        ce, cv = f"{name}_edan", f"{name}_visita"
        if ce in df.columns and cv in df.columns:
            df[f"{name}_total"] = np.fmax(_as_num(ce), _as_num(cv))

    def _single_source_trust(row) -> float:
        t = 0.20
        addr = canonicalize_for_match(row["direccion_unificada"])
        if addr and address_to_vector(addr)[0] != 0:
            t += 0.10
        if row.get("coords_unificadas"):
            t += 0.10
        if str(row.get("barrio_unificado")).strip() not in {"", "-", "nan", "None"}:
            t += 0.05
        return round(t, 2)

    trust_score = df["trust"].where(
        df["trust"].notna(), df.apply(_single_source_trust, axis=1))
    df.insert(1, "trust_score", trust_score.astype(float))
    return df


def build_confiable(df_integrado) -> pd.DataFrame:
    """High-trust subset (trust_score > 0.7) with redundant/synthetic cols dropped."""
    return (df_integrado[df_integrado["trust_score"] > 0.7]
            .drop(columns=_REDUNDANT, errors="ignore").copy())


def build_consolidada(df_integrado) -> pd.DataFrame:
    """Lean deliverable table: the essential unified columns + reliability score
    + both source ids, without the synthetic pipeline internals. One row per
    integrated record (edan+visita | solo_edan | solo_visita)."""
    keep = [
        "fuente", "trust_score", "match_method",
        "sitio_id", "visita_id",
        "direccion_unificada", "barrio_unificado", "comuna_unificada",
        "lat", "lon", "coords_unificadas",
        "tipo_estructura_edan", "tipo_estructura_visita",
        "nombre_estructura", "punto_referencia",
        "nivel_riesgo", "estado", "estado_estructura", "requiere_demolicion",
        "n_fallecidos_total", "n_atrapamientos_total", "n_rescatados_total",
        "descripcion_edan", "descripcion_visita",
    ]
    cols = [c for c in keep if c in df_integrado.columns]
    out = df_integrado[cols].copy()
    out = out.sort_values(["fuente", "trust_score"], ascending=[True, False])
    return out
