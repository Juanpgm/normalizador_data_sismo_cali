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

    # ONE coordinate per record, always the most trustworthy one available.
    # Ranking: exif (instrument) > EDAN typed > visita typed > EDAN geocoded >
    # visita geocoded. Measured and typed values are independent data; a
    # geocoded one merely derives from the address already in the table.
    # `coords_fuente` records which source won, so every row stays auditable.
    _empty = pd.Series([None] * len(df), index=df.index)
    _pts_e = df["coords_edan"].map(parse_latlon) if "coords_edan" in df else _empty
    _pts_v = df["coords_visita"].map(parse_latlon) if "coords_visita" in df else _empty

    def _fuente_of(side_col, plain_ok, pts, typed_label):
        """Provenance series for one side. The merge suffixes the column when
        both tables carry it; a bare `coords_fuente` is historically the visita
        one. Rows with a parseable point but no label default to 'typed'."""
        if side_col in df:
            s = df[side_col].astype(str)
        elif plain_ok and "coords_fuente" in df:
            s = df["coords_fuente"].astype(str)
        else:
            s = pd.Series("", index=df.index)
        s = s.where(~s.isin(["nan", "None"]), "")
        return s.mask(s.eq("") & pts.notna(), typed_label)

    _fe = _fuente_of("coords_fuente_edan", False, _pts_e, "edan")
    _fv = _fuente_of("coords_fuente_visita", True, _pts_v, "visita")
    _prec_e = pd.to_numeric(
        df.get("coords_precision_m_edan", _empty), errors="coerce")
    _prec_v = pd.to_numeric(
        df.get("coords_precision_m_visita", df.get("coords_precision_m", _empty)),
        errors="coerce")

    _c_exif = _pts_v.notna() & _fv.eq("exif")
    _c_edan = _pts_e.notna() & _fe.eq("edan")
    _c_vis = _pts_v.notna() & _fv.eq("visita")
    _c_gc_e = _pts_e.notna() & _fe.eq("geocode")
    _c_gc_v = _pts_v.notna() & _fv.eq("geocode")

    _pts = _pts_v.where(_c_exif,
           _pts_e.where(_c_edan,
           _pts_v.where(_c_vis,
           _pts_e.where(_c_gc_e,
           _pts_v.where(_c_gc_v)))))
    _conds = [_c_exif, _c_edan, _c_vis, _c_gc_e, _c_gc_v]
    _fuente = np.select(_conds, ["exif", "edan", "visita", "geocode", "geocode"],
                        default="")
    _precision = np.select(
        _conds,
        [_prec_v, _prec_e, _prec_v, _prec_e, _prec_v], default=np.nan)

    # The raw per-source columns are consumed here; drop them before inserting
    # the unified ones so the names are free and no second coordinate survives.
    df = df.drop(columns=["coords_fuente", "coords_precision_m",
                          "coords_fuente_edan", "coords_fuente_visita",
                          "coords_precision_m_edan", "coords_precision_m_visita"],
                 errors="ignore")

    # isinstance, not truthiness: the .where chain fills losers with NaN, and
    # NaN is truthy — `if p` would happily subscript a float.
    df.insert(4, "coords", _pts.map(
        lambda p: f"{p[0]:.6f}, {p[1]:.6f}" if isinstance(p, tuple) else None))
    df.insert(5, "coords_fuente", _fuente)
    df.insert(6, "coords_precision_m", pd.Series(_precision, index=df.index))

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
        if row.get("coords"):
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
        "sitio_id", "visita_id", "consecutivo_edan",
        "direccion_unificada", "barrio_unificado", "comuna_unificada",
        "coords", "coords_fuente", "coords_precision_m",
        "tipo_estructura_edan", "tipo_estructura_visita",
        "nombre_estructura", "punto_referencia",
        "nivel_riesgo", "estado", "estado_estructura", "requiere_demolicion",
        "n_fallecidos_total", "n_atrapamientos_total", "n_rescatados_total",
        "descripcion_edan", "descripcion_visita",
        "n_evidencias", "evidencia_soporte",
    ]
    cols = [c for c in keep if c in df_integrado.columns]
    out = df_integrado[cols].copy()
    out = out.sort_values(["fuente", "trust_score"], ascending=[True, False])
    return out
