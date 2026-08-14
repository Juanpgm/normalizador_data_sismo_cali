"""One coordinate column per record, and the Drive links survive to the deliverable."""
import pandas as pd

from integracion.integrate import build_consolidada, build_integrado, build_master


def _frames(coords_visita, coords_fuente, coords_edan="3.400000, -76.500000",
            fuente_edan=None):
    if fuente_edan is None:
        fuente_edan = "edan" if coords_edan else ""
    edan = pd.DataFrame({
        "sitio_id": ["S1"], "direccion_norm": ["CL 5 # 1-1"],
        "barrio_vereda": ["Centro"], "comuna_corregimiento": ["3"],
        "coords": [coords_edan], "coords_fuente": [fuente_edan],
        "coords_precision_m": [15.0 if fuente_edan == "geocode" else pd.NA],
        "integration_handshake": ["h"],
        "n_fallecidos": ["0"], "n_atrapamientos": ["0"], "n_rescatados": ["0"],
        "consecutivo_edan": ["17"], "descripcion": ["edan"],
        "tipo_estructura": ["Casa"],
    })
    visitas = pd.DataFrame({
        "visita_id": ["V1"], "direccion_norm": ["CL 5 # 1-1, Centro, Cali"],
        "barrio_vereda": ["Centro"], "comuna_corregimiento": ["3"],
        "coords": [coords_visita], "coords_fuente": [coords_fuente],
        "coords_precision_m": [11.0], "integration_handshake": ["h"],
        "n_fallecidos": ["0"], "n_atrapamientos": ["0"], "n_rescatados": ["0"],
        "descripcion": ["visita"], "tipo_estructura": ["Casa"],
        "evidencia_soporte": ["https://drive.google.com/open?id=" + "A" * 25],
        "n_evidencias": [1],
    })
    match = pd.DataFrame({"visita_id": ["V1"], "sitio_id": ["S1"],
                          "match_method": ["handshake"], "match_score": [100.0],
                          "trust": [0.95]})
    return build_integrado(build_master(edan, match, visitas))


def test_exif_coordinate_wins_over_edan():
    df = _frames("3.410000, -76.510000", "exif")
    assert df["coords"].iloc[0] == "3.410000, -76.510000"
    assert df["coords_fuente"].iloc[0] == "exif"
    assert df["coords_precision_m"].iloc[0] == 11.0


def test_edan_wins_over_a_typed_visita_coordinate():
    df = _frames("3.410000, -76.510000", "visita")
    assert df["coords"].iloc[0] == "3.400000, -76.500000"
    assert df["coords_fuente"].iloc[0] == "edan"
    assert pd.isna(df["coords_precision_m"].iloc[0])


def test_typed_visita_coordinate_used_when_edan_has_none():
    df = _frames("3.410000, -76.510000", "visita", coords_edan="")
    assert df["coords"].iloc[0] == "3.410000, -76.510000"
    assert df["coords_fuente"].iloc[0] == "visita"


def test_typed_visita_beats_geocoded_edan():
    """Typed data is an independent datum; geocode derives from the address."""
    df = _frames("3.410000, -76.510000", "visita",
                 coords_edan="3.420000, -76.520000", fuente_edan="geocode")
    assert df["coords"].iloc[0] == "3.410000, -76.510000"
    assert df["coords_fuente"].iloc[0] == "visita"


def test_geocoded_edan_beats_geocoded_visita_and_fills_the_gap():
    df = _frames("3.410000, -76.510000", "geocode",
                 coords_edan="3.420000, -76.520000", fuente_edan="geocode")
    assert df["coords"].iloc[0] == "3.420000, -76.520000"
    assert df["coords_fuente"].iloc[0] == "geocode"
    assert df["coords_precision_m"].iloc[0] == 15.0


def test_geocoded_visita_used_as_last_resort():
    df = _frames("3.410000, -76.510000", "geocode", coords_edan="",
                 fuente_edan="")
    assert df["coords"].iloc[0] == "3.410000, -76.510000"
    assert df["coords_fuente"].iloc[0] == "geocode"


def test_consolidada_has_exactly_one_coordinate_column():
    consolidada = build_consolidada(_frames("3.410000, -76.510000", "exif"))
    coord_cols = [c for c in consolidada.columns if c.startswith("coords")]
    assert coord_cols == ["coords", "coords_fuente", "coords_precision_m"]
    for gone in ("lat", "lon", "coords_unificadas", "coords_edan", "coords_visita"):
        assert gone not in consolidada.columns


def test_consolidada_keeps_the_drive_links():
    consolidada = build_consolidada(_frames("3.410000, -76.510000", "exif"))
    assert "evidencia_soporte" in consolidada.columns
    assert "drive.google.com" in consolidada["evidencia_soporte"].iloc[0]
    assert consolidada["n_evidencias"].iloc[0] == 1
