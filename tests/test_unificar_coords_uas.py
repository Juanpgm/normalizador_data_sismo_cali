import pandas as pd

from unificar_coords_uas import build_unificado, folder_to_address


def test_folder_to_address():
    assert folder_to_address("28. Calle 9 & Carrera 18") == "Calle 9 con Carrera 18"
    assert folder_to_address("SGRED KR 56 CL 3") == "KR 56 CL 3"
    assert folder_to_address("CL 5 #36-08 HUV") == "CL 5 # 36-08 HUV"
    assert folder_to_address("CR 62 CON 3 (FRENTE A PETER PAN)") == "CR 62 CON 3"
    # address inside the parenthesis ("N) code (address)" naming)
    assert folder_to_address("1) L2 (Cl 3D KR 67)") == "Cl 3D KR 67"
    # not addresses: no road word + number
    assert folder_to_address("Fotogrametria") == ""
    assert folder_to_address("Centro (Aristi, corficolombiana)") == ""
    assert folder_to_address("2) K-2 (Torres del limonar)") == ""


def test_build_unificado_centroid_only(monkeypatch):
    import unificar_coords_uas as m
    monkeypatch.setattr(m, "_geocode_places", lambda addrs: {})
    df = pd.DataFrame({
        "carpeta_vuelo": ["V1"] * 3,
        "subcarpeta": ["CL 9 KR 42/sub", "CL 9 KR 42", "Fotogrametria"],
        "fuente": ["exif_drive", "exif_drive", "sin_gps"],
        "lat": ["3.45", "3.4502", ""],
        "lon": ["-76.53", "-76.5302", ""],
    })
    out = build_unificado(df)
    assert len(out) == 2
    site = out[out["lugar"] == "CL 9 KR 42"].iloc[0]
    assert site["fuente"] == "centroide_fotos" and site["n_fotos_gps"] == 2
    assert abs(site["lat"] - 3.4501) < 1e-4
    empty = out[out["lugar"] == "Fotogrametria"].iloc[0]
    assert empty["fuente"] == "sin_coordenada" and pd.isna(empty["lat"])
