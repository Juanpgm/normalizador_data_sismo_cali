"""
Integration metrics + progress report. Turns a match table / integrated table
into an auditable set of numbers used both for the human report and for the
progress tests (tests/test_pipeline_progress.py).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .coords import parse_latlon


def coords_coverage(df) -> dict:
    parseable = sum(1 for x in df["coords"] if parse_latlon(x))
    return {"total": len(df), "con_coords_wgs84": parseable,
            "pct": round(100 * parseable / max(len(df), 1), 1)}


def match_metrics(match_table, df_edan, df_visitas, bridge_info=None,
                  exif_info=None, geo_key_info=None, geocode_info=None) -> dict:
    matched = match_table["sitio_id"].notna()
    by_method = match_table.loc[matched, "match_method"].value_counts().to_dict()
    m = {
        "visitas_total": int(len(match_table)),
        "visitas_emparejadas": int(matched.sum()),
        "tasa_match_pct": round(100 * matched.sum() / max(len(match_table), 1), 1),
        "sitios_edan_total": int(df_edan["sitio_id"].nunique()),
        "sitios_edan_con_visita": int(match_table.loc[matched, "sitio_id"].nunique()),
        "por_metodo": {k: int(v) for k, v in by_method.items()},
        "coords_edan": coords_coverage(df_edan),
        "coords_visitas": coords_coverage(df_visitas),
    }
    if "trust" in match_table.columns:
        tr = match_table.loc[matched, "trust"]
        if len(tr):
            m["trust"] = {"mean": round(float(tr.mean()), 3),
                          "min": round(float(tr.min()), 3),
                          "median": round(float(tr.median()), 3)}
    if bridge_info:
        m["spatial_bridge"] = {k: bridge_info[k] for k in
                               ("mode", "n_parcels", "edan_with_parcel",
                                "visitas_with_parcel", "added") if k in bridge_info}
    if exif_info:
        m["coords_exif"] = dict(exif_info)
    if geocode_info:
        m["geocode"] = dict(geocode_info)
    if geo_key_info:
        m["geo_key"] = dict(geo_key_info)
    if "coords_fuente" in df_visitas.columns:
        m["coords_visitas_por_fuente"] = {
            str(k): int(v) for k, v in
            df_visitas["coords_fuente"].astype(str).replace("", "ninguna")
            .value_counts().items()}
    return m


def integrado_metrics(df_integrado) -> dict:
    return {
        "registros": int(len(df_integrado)),
        "columnas": int(df_integrado.shape[1]),
        "por_fuente": {k: int(v) for k, v in df_integrado["fuente"].value_counts().items()},
        "coords_estandarizadas": int(sum(1 for x in df_integrado["coords"] if parse_latlon(x))),
        "trust_score": {
            "mean": round(float(df_integrado["trust_score"].mean()), 3),
            "min": round(float(df_integrado["trust_score"].min()), 3),
            "max": round(float(df_integrado["trust_score"].max()), 3),
        },
    }


def build_report(match_table, df_edan, df_visitas, df_integrado, bridge_info=None,
                 exif_info=None, geo_key_info=None, geocode_info=None) -> dict:
    return {
        "matching": match_metrics(match_table, df_edan, df_visitas, bridge_info,
                                  exif_info, geo_key_info, geocode_info),
        "integracion": integrado_metrics(df_integrado),
    }


def flatten(d: dict, prefix: str = "") -> dict:
    """Nested report dict → flat ``{"matching.trust.mean": 0.912}`` mapping.
    Shared by the Excel exporter and the Sheets exporter."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def save_report(report: dict, path) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def print_report(report: dict) -> None:
    mt = report["matching"]
    it = report["integracion"]
    print("=" * 60)
    print("INTEGRACIÓN EDAN ↔ VISITAS — MÉTRICAS")
    print("=" * 60)
    print(f"Visitas emparejadas : {mt['visitas_emparejadas']} / {mt['visitas_total']} "
          f"({mt['tasa_match_pct']}%)")
    print(f"Sitios EDAN c/visita : {mt['sitios_edan_con_visita']} / {mt['sitios_edan_total']}")
    print(f"Coords WGS84 EDAN    : {mt['coords_edan']['con_coords_wgs84']}/{mt['coords_edan']['total']} "
          f"({mt['coords_edan']['pct']}%)")
    print(f"Coords WGS84 Visitas : {mt['coords_visitas']['con_coords_wgs84']}/{mt['coords_visitas']['total']} "
          f"({mt['coords_visitas']['pct']}%)")
    if "coords_exif" in mt:
        ex = mt["coords_exif"]
        print(f"Coords desde EXIF    : {ex['visitas_con_gps']} visitas · "
              f"{ex['fotos_gps_usadas']} fotos · dispersión mediana "
              f"{ex['dispersion_mediana_m']} m · {ex['omitidas_por_confiables']} omitidas")
    if "geocode" in mt:
        ge = mt["geocode"]
        print(f"Geocodificación      : {ge['cache_hits']} de cache · "
              f"{ge['llamadas_api']} llamadas API · +{ge['aceptadas_nuevas']} "
              f"aceptadas · {ge['rechazadas_nuevas']} rechazadas")
    if "spatial_bridge" in mt:
        sb = mt["spatial_bridge"]
        print(f"Puente espacial      : modo={sb['mode']} · +{sb['added']} matches · "
              f"{sb['n_parcels']} predios")
    if "geo_key" in mt:
        gk = mt["geo_key"]
        print(f"Llave por coordenada : +{gk['added']} matches · "
              f"{gk['rechazadas_ambiguas']} rechazadas por ambigüedad")
    print("\nMatches por método:")
    for k, v in sorted(mt["por_metodo"].items(), key=lambda x: -x[1]):
        print(f"  {k:16s} {v}")
    if "trust" in mt:
        print(f"\nTrust matches: media={mt['trust']['mean']} min={mt['trust']['min']} "
              f"mediana={mt['trust']['median']}")
    print(f"\nIntegrado: {it['registros']} registros × {it['columnas']} columnas")
    for k, v in it["por_fuente"].items():
        print(f"  {k:16s} {v}")
    print(f"Coords estandarizadas: {it['coords_estandarizadas']}")
    print("=" * 60)
