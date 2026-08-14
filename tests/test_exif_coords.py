"""EXIF-coordinate harvesting: link parsing, GPS→decimal, non-destructive fill."""
import pandas as pd

from integracion.exif_coords import (apply_to_visitas, build_fileid_to_visita,
                                      extract_file_ids, gps_ifd_to_latlon)


def test_extract_file_ids_multiple():
    cell = ("https://drive.google.com/open?id=1ENF1wplMbDYguzgsJ9ji4EhtfFAyUnGD, "
            "https://drive.google.com/open?id=12M1jK9kLE7Z8ka7QcU8I2jXjc7Gw-jj6")
    ids = extract_file_ids(cell)
    assert ids == ["1ENF1wplMbDYguzgsJ9ji4EhtfFAyUnGD", "12M1jK9kLE7Z8ka7QcU8I2jXjc7Gw-jj6"]


def test_extract_file_ids_empty():
    assert extract_file_ids("") == []
    assert extract_file_ids("nan") == []


def test_build_fileid_to_visita():
    df = pd.DataFrame({
        "visita_id": ["V1", "V2"],
        "evidencia_soporte": ["https://drive.google.com/open?id=AAAAAAAAAAAAAAAAAAAAAA",
                              "https://drive.google.com/open?id=BBBBBBBBBBBBBBBBBBBBBB"],
    })
    m = build_fileid_to_visita(df)
    assert m["AAAAAAAAAAAAAAAAAAAAAA"] == "V1"
    assert m["BBBBBBBBBBBBBBBBBBBBBB"] == "V2"


def test_gps_ifd_to_latlon_cali_point():
    # 3°27'24.5"N, 76°32'35.2"W  (a Cali coordinate)
    gps = {1: "N", 2: ((3, 1), (27, 1), (245, 10)),
           3: "W", 4: ((76, 1), (32, 1), (352, 10))}
    latlon = gps_ifd_to_latlon(gps)
    assert latlon is not None
    lat, lon = latlon
    assert abs(lat - (3 + 27/60 + 24.5/3600)) < 1e-4
    assert lon < 0  # West → negative
    assert abs(lon + (76 + 32/60 + 35.2/3600)) < 1e-4


def test_gps_ifd_missing_returns_none():
    assert gps_ifd_to_latlon({}) is None
    assert gps_ifd_to_latlon({1: "N"}) is None


def test_apply_to_visitas_non_destructive():
    df = pd.DataFrame({
        "visita_id": ["V1", "V2"],
        "coords": ["", "3.400000, -76.500000"],  # V1 empty, V2 already set
    })
    harvested = pd.DataFrame({
        "visita_id": ["V1", "V2"],
        "lat": [3.44, 3.99], "lon": [-76.53, -76.99], "source": ["exif", "exif"],
    })
    out = apply_to_visitas(df, harvested)
    assert out.loc[out["visita_id"] == "V1", "coords"].iloc[0] == "3.440000, -76.530000"
    # existing coordinate must NOT be overwritten
    assert out.loc[out["visita_id"] == "V2", "coords"].iloc[0] == "3.400000, -76.500000"
