"""Offline self-check for integracion.api_visitados.casos_to_tabla.

No live API call. Run with `python test_api_visitados.py`.
"""
from __future__ import annotations

import pandas as pd

from integracion.api_visitados import TABLA_COLS, casos_to_tabla
from asignar_f3 import resolve_ts, riesgo_value

CASO_FULL = {
    "id": "REG-0001",
    "estado": "critico",
    "estadoEtiqueta": "Visitado Crítico",
    "direccion": "CL 46 # 45-10, Centro, Cali",
    "lat": 3.47798,
    "lng": -76.49267,
    "placeId": "manual:3.47798,-76.49267",
    "comuna": 5,
    "comunaEtiqueta": "Comuna 05",
    "barrio": "Centro",
    "resumenUnidad": "Edificio Torre A",
    "ingreso": {
        "descripcion": "Colapso parcial de fachada",
        "tipoColapso": "A",
        "tipoColapsoEtiqueta": "COLAPSO TOTAL",
        "tipoInmueble": "residencial",
        "tipoInmuebleEtiqueta": "Residencial",
        "nombreEdificio": "Torre A",
        "contacto": {"nombre": "Juan", "telefono": "300", "cedula": "1"},
        "creado_utc": 1722470400000,
    },
    "contacto": {"nombre": "Juan", "telefono": "300", "cedula": "1"},
    "inmueble": {
        "tipoInmueble": "residencial",
        "tipoInmuebleEtiqueta": "Residencial",
        "nombreEdificio": "Torre A",
    },
    "operarioIngreso": {"id": "op1", "nombre": "Op", "correo": "op@x.com"},
    "tecnicoVerificacion": {"id": "tec1", "nombre": "Tec"},
    "evaluacion": {
        "id": "EVAL-0001",
        "creado_utc": 1722480000000,
        "tipoColapso": "A",
        "tipoColapsoEtiqueta": "COLAPSO TOTAL",
        "tipoInmueble": "residencial",
        "tipoInmuebleEtiqueta": "Residencial",
        "pisosSobreNivel": 5,
        "sotanos": 0,
        "danos": [],
        "victimas": {"fallecidos": 1, "atrapados": 2, "rescatados": 3},
        "habitabilidad": "not_habitable",
        "habitabilidadEtiqueta": "No habitable",
        "conceptoTecnico": "Estructura colapsada, riesgo inminente",
    },
    "mensajes": [],
}

CASO_NULLS = {
    "id": "REG-0002",
    "estado": "critico",
    "estadoEtiqueta": "Visitado Crítico",
    "direccion": "KR 1 # 2-3",
    "lat": None,
    "lng": None,
    "placeId": None,
    "comuna": None,
    "comunaEtiqueta": None,
    "barrio": None,
    "resumenUnidad": None,
    "ingreso": {
        "descripcion": None,
        "tipoColapso": None,
        "tipoColapsoEtiqueta": None,
        "tipoInmueble": None,
        "tipoInmuebleEtiqueta": None,
        "nombreEdificio": None,
        "creado_utc": 1722470400000,
    },
    "contacto": None,
    "inmueble": {
        "tipoInmueble": None,
        "tipoInmuebleEtiqueta": None,
        "nombreEdificio": None,
    },
    "operarioIngreso": None,
    "tecnicoVerificacion": None,
    "evaluacion": {
        "id": "EVAL-0002",
        "creado_utc": 1722480000000,
        "tipoColapso": "B",
        "tipoColapsoEtiqueta": "RIESGO COLAPSO",
        "tipoInmueble": None,
        "tipoInmuebleEtiqueta": None,
        "danos": [],
        "victimas": {"fallecidos": None, "atrapados": None, "rescatados": None},
        "habitabilidad": None,
        "habitabilidadEtiqueta": None,
        "conceptoTecnico": None,
    },
    "mensajes": [],
}


# Same report (id REG-0001) as CASO_FULL but an earlier, superseded evaluation
# — the API returns one element per evaluation, not per report.
CASO_FULL_OLDER_DUP = {
    **CASO_FULL,
    "evaluacion": {
        **CASO_FULL["evaluacion"],
        "id": "EVAL-0000",
        "creado_utc": 1700000000000,  # older than CASO_FULL's 1722480000000
        "conceptoTecnico": "Evaluacion anterior, superada",
    },
}


def test_casos_to_tabla():
    df = casos_to_tabla([CASO_FULL, CASO_NULLS])
    assert list(df.columns) == TABLA_COLS
    assert len(df) == 2
    # every cell is a string (the _read_tab raw-text contract)
    for col in df.columns:
        assert df[col].map(type).eq(str).all(), col

    r0 = df.iloc[0]
    assert r0["registro_id"] == "REG-0001"
    assert r0["fuente"] == "api_visitados_criticos"
    assert r0["sitio_id"] == "manual:3.47798,-76.49267"
    assert r0["visita_id"] == "EVAL-0001"
    assert r0["direccion_unificada"] == "CL 46 # 45-10, Centro, Cali"
    assert r0["barrio_unificado"] == "Centro"
    assert r0["comuna_unificada"] == "Comuna 05"
    assert r0["coords_unificadas"] == "3.477980, -76.492670"
    assert r0["tipo_estructura_edan"] == "Residencial"
    assert r0["tipo_estructura_visita"] == "Residencial"
    assert r0["nombre_estructura"] == "Torre A"
    assert r0["nivel_riesgo"] == "COLAPSO TOTAL"
    assert r0["estado"] == "Visitado Crítico"
    assert r0["estado_estructura"] == "No habitable"
    assert r0["requiere_demolicion"] == "si"
    assert r0["n_fallecidos_total"] == "1"
    assert r0["n_atrapamientos_total"] == "2"
    assert r0["n_rescatados_total"] == "3"
    assert r0["descripcion_edan"] == "Colapso parcial de fachada"
    assert r0["descripcion_visita"] == "Estructura colapsada, riesgo inminente"

    r1 = df.iloc[1]
    assert r1["registro_id"] == "REG-0002"
    assert r1["sitio_id"] == ""
    assert r1["coords_unificadas"] == ""       # lat/lng both null
    assert r1["comuna_unificada"] == ""        # comunaEtiqueta null, comuna null
    assert r1["nombre_estructura"] == ""       # nombreEdificio and resumenUnidad null
    assert r1["nivel_riesgo"] == "RIESGO COLAPSO"
    assert r1["requiere_demolicion"] == ""     # tipoColapso B, not A
    assert r1["n_fallecidos_total"] == ""
    assert r1["n_atrapamientos_total"] == ""
    assert r1["n_rescatados_total"] == ""
    assert r1["tipo_estructura_edan"] == ""
    assert r1["tipo_estructura_visita"] == ""


def test_empty_casos():
    df = casos_to_tabla([])
    assert list(df.columns) == TABLA_COLS
    assert len(df) == 0


def test_evaluacion_creado_utc_column():
    """REL-001: the API's own evaluation timestamp must survive as an
    epoch-ms string — it's what resolve_ts() keys off in asignar_f3."""
    df = casos_to_tabla([CASO_FULL, CASO_NULLS])
    assert df.iloc[0]["evaluacion_creado_utc"] == "1722480000000"
    assert df.iloc[1]["evaluacion_creado_utc"] == "1722480000000"


def test_riesgo_value_long_collapse_label():
    """REL-002: real API nivel_riesgo values are long labels like
    'RIESGO COLAPSO: Edificio daños graves...' — must score as 'alto'."""
    label = "RIESGO COLAPSO: Edificio daños graves, ladeados, a punto de caer"
    assert riesgo_value(label) == riesgo_value("alto") == 1.0


def test_dedup_keeps_latest_evaluacion_per_registro():
    """The API is one-element-per-evaluation: a report with repeat visits
    appears more than once under the same registro_id. Only the row with the
    greatest evaluacion_creado_utc must survive (id_asignacion hashes
    registro_id, so duplicates would collide downstream)."""
    df = casos_to_tabla([CASO_FULL, CASO_FULL_OLDER_DUP, CASO_NULLS])
    assert len(df) == 2  # REG-0001 collapsed to one row, REG-0002 untouched
    assert sorted(df["registro_id"]) == ["REG-0001", "REG-0002"]
    r0 = df[df["registro_id"] == "REG-0001"].iloc[0]
    assert r0["visita_id"] == "EVAL-0001"                # the newer evaluation
    assert r0["evaluacion_creado_utc"] == "1722480000000"
    assert r0["descripcion_visita"] == "Estructura colapsada, riesgo inminente"


def test_resolve_ts_from_api_row():
    """REL-001: resolve_ts must derive age from evaluacion_creado_utc for an
    API-shaped row, and still fall back to the Visitas-sheet join otherwise."""
    row = {"evaluacion_creado_utc": "1722480000000", "visita_id": "EVAL-0001"}
    ts = resolve_ts(row, {})
    assert ts is not None and ts.tzinfo is None
    # epoch ms UTC -> naive Bogota-local wall clock (UTC-5)
    assert ts == pd.Timestamp(1722480000000, unit="ms") - pd.Timedelta(hours=5)

    fallback = pd.Timestamp("2026-08-10 08:00:00")
    row_form = {"evaluacion_creado_utc": "", "visita_id": "form-123"}
    assert resolve_ts(row_form, {"form-123": fallback}) == fallback


if __name__ == "__main__":
    test_casos_to_tabla()
    test_empty_casos()
    test_evaluacion_creado_utc_column()
    test_riesgo_value_long_collapse_label()
    test_dedup_keeps_latest_evaluacion_per_registro()
    test_resolve_ts_from_api_row()
    print("selfcheck ok")
