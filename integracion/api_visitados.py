"""Client for the "visitados-criticos" REST API — replaces the `tabla_integrada`
Google Sheets tab as the data source for critical-case (collapse A/B) records.

Auth is HTTP Basic (user + password). The password lives in the environment
(`VISITADOS_API_PASS`); `.env` is loaded the same way `integracion.config`
loads it, so no extra bootstrap is needed by callers that already import
`integracion.config` (both consumer scripts do, transitively).

`casos_to_tabla` builds a DataFrame with the same raw-text contract as
`_read_tab` (Google Sheets `get_all_values()`): every cell is a string, ""
when the source value is null/missing.
"""
from __future__ import annotations

import os
import time

import pandas as pd
import requests

from . import config  # noqa: F401 - import ensures .env is loaded

API_URL = "https://atencionsismo.cali.gov.co/api/operario/reports/visitados-criticos"
USER_ENV = "VISITADOS_API_USER"
PASS_ENV = "VISITADOS_API_PASS"
DEFAULT_USER = "juanp.gzmz@gmail.com"

TABLA_COLS = [
    "registro_id", "fuente", "sitio_id", "visita_id",
    "direccion_unificada", "barrio_unificado", "comuna_unificada",
    "coords_unificadas", "tipo_estructura_edan", "tipo_estructura_visita",
    "nombre_estructura", "nivel_riesgo", "estado", "estado_estructura",
    "requiere_demolicion", "n_fallecidos_total", "n_atrapamientos_total",
    "n_rescatados_total", "descripcion_edan", "descripcion_visita",
    "evaluacion_creado_utc",
]


def fetch_casos(desde_utc: int = 0, hasta_utc: int | None = None) -> list[dict]:
    """GET the critical-cases list for [desde_utc, hasta_utc] (Unix ms UTC)."""
    if hasta_utc is None:
        hasta_utc = int(time.time() * 1000)
    user = os.environ.get(USER_ENV, "").strip() or DEFAULT_USER
    password = os.environ.get(PASS_ENV, "").strip()
    if not password:
        raise RuntimeError(f"{PASS_ENV} is not set; cannot authenticate against the API.")
    resp = requests.get(API_URL, params={"desde_utc": desde_utc, "hasta_utc": hasta_utc},
                        auth=(user, password), timeout=60)
    resp.raise_for_status()
    return resp.json()["casos"]


def _s(v) -> str:
    """Stringify a scalar, mapping None/missing to "" (the _read_tab contract)."""
    return "" if v is None else str(v)


def _coords(lat, lng) -> str:
    """'lat, lon' formatted like coords.coords_to_wgs84, parseable by
    integracion.coords.parse_latlon (asignar_f3's coords fallback)."""
    if lat is None or lng is None:
        return ""
    return f"{float(lat):.6f}, {float(lng):.6f}"


def casos_to_tabla(casos: list[dict]) -> pd.DataFrame:
    """One row per caso, all-string columns matching the tabla_integrada shape
    consumed by integrar_f3.py and asignar_f3.py."""
    rows = []
    for c in casos:
        ingreso = c.get("ingreso") or {}
        inmueble = c.get("inmueble") or {}
        evaluacion = c.get("evaluacion") or {}
        victimas = evaluacion.get("victimas") or {}

        comuna_etiqueta = c.get("comunaEtiqueta") or _s(c.get("comuna"))
        tipo_visita = (evaluacion.get("tipoInmuebleEtiqueta")
                       or inmueble.get("tipoInmuebleEtiqueta"))
        # requiere_demolicion: total collapse (tipoColapso == "A") is the only
        # API signal that maps to demolition; matches asignar_f3.demolicion_value.
        demolicion = "si" if evaluacion.get("tipoColapso") == "A" else ""

        rows.append({
            "registro_id": _s(c.get("id")),
            "fuente": "api_visitados_criticos",
            "sitio_id": _s(c.get("placeId")),
            "visita_id": _s(evaluacion.get("id")),
            "direccion_unificada": _s(c.get("direccion")),
            "barrio_unificado": _s(c.get("barrio")),
            "comuna_unificada": _s(comuna_etiqueta),
            "coords_unificadas": _coords(c.get("lat"), c.get("lng")),
            "tipo_estructura_edan": _s(ingreso.get("tipoInmuebleEtiqueta")),
            "tipo_estructura_visita": _s(tipo_visita),
            "nombre_estructura": _s(inmueble.get("nombreEdificio") or c.get("resumenUnidad")),
            "nivel_riesgo": _s(evaluacion.get("tipoColapsoEtiqueta")),
            "estado": _s(c.get("estadoEtiqueta")),
            "estado_estructura": _s(evaluacion.get("habitabilidadEtiqueta")),
            "requiere_demolicion": demolicion,
            "n_fallecidos_total": _s(victimas.get("fallecidos")),
            "n_atrapamientos_total": _s(victimas.get("atrapados")),
            "n_rescatados_total": _s(victimas.get("rescatados")),
            "descripcion_edan": _s(ingreso.get("descripcion")),
            "descripcion_visita": _s(evaluacion.get("conceptoTecnico")),
            # Epoch ms (UTC) when the evaluation was saved — the API's own
            # timestamp, used by asignar_f3 for record age (visita_id here is
            # the evaluation id, disjoint from the Visitas Google-Form ids).
            "evaluacion_creado_utc": _s(evaluacion.get("creado_utc")),
        })

    # The API returns one element per EVALUATION, not per report: a report
    # with repeat visits appears several times under the same registro_id
    # (up to 4 observed live). Downstream code assumes one row per
    # registro_id (id_asignacion hashes it), so keep only the most recent
    # evaluation per report; missing evaluacion_creado_utc sorts lowest.
    latest: dict[str, dict] = {}
    for r in rows:
        key = r["registro_id"]
        ts = int(r["evaluacion_creado_utc"] or -1)
        if key not in latest or ts > int(latest[key]["evaluacion_creado_utc"] or -1):
            latest[key] = r
    return pd.DataFrame(latest.values(), columns=TABLA_COLS)


def fetch_tabla(desde_utc: int = 0, hasta_utc: int | None = None) -> pd.DataFrame:
    """Convenience wrapper: fetch_casos + casos_to_tabla."""
    return casos_to_tabla(fetch_casos(desde_utc, hasta_utc))
