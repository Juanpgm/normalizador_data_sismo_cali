"""Address normalization, handshake and 3D vector behaviour."""
import numpy as np

from integracion.normalization import (address_to_vector, make_handshake,
                                        normalize_address)


def test_road_types_to_igac_codes():
    assert normalize_address("Calle 80 No. 45-23").startswith("CL 80 # 45-23")
    assert normalize_address("Kra 10 Num 15-20").startswith("KR 10 # 15-20")
    assert normalize_address("Carrera 100").startswith("KR 100")
    assert normalize_address("Avenida 5 Norte # 22-40").startswith("AV 5")


def test_carrera_code_is_kr_not_cr():
    assert normalize_address("CR 4 # 5-6").startswith("KR 4")


def test_complement_titlecased_nomenclature_upper():
    out = normalize_address("Calle 5 # 43A-15, barrio el LIDO")
    nom, comp = out.split(",", 1)
    assert nom.strip() == nom.strip().upper()
    assert comp.strip() == "Barrio El Lido"


def test_empty_and_dash_passthrough():
    assert normalize_address(None) == ""
    assert normalize_address("-") == "-"
    assert normalize_address("") == ""


def test_handshake_strong_and_weak():
    assert make_handshake("CL 5 # 43A-15, El Lido, Cali") == "CL5#43A-15"
    # a corner reference has no '#': weak fingerprint
    assert "#" not in make_handshake("KR 72 CON CL 11, La Hacienda, Cali")


def test_vector_letter_and_bis_semantics():
    base = address_to_vector("CL 5 # 43-15")
    letter = address_to_vector("CL 5A # 43-15")
    bis = address_to_vector("CL 5 BIS # 43-15")
    # letter suffix nudges the road dimension by ~0.01, BIS by 0.50
    assert np.isclose(letter[0] - base[0], 0.01, atol=1e-3)
    assert np.isclose(bis[0] - base[0], 0.50, atol=1e-3)


def test_vector_distinct_roads_far_apart():
    cl = address_to_vector("CL 5 # 43-15")
    kr = address_to_vector("KR 5 # 43-15")
    assert abs(cl[0] - kr[0]) >= 1000
