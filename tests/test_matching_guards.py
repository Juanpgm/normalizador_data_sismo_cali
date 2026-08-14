"""Precision guards: canonicalization, barrio agreement, numeric coherence,
corner fingerprint. These are what keep recall gains from buying false positives."""
from integracion.matching import (barrio_ok, canonicalize_for_match, coherent,
                                   corner_key)
from integracion.normalization import address_to_vector


def test_canonicalize_strips_unit_noise():
    out = canonicalize_for_match("KR 60A # 11B-31 APTO 203B, Santa Anita, Cali")
    assert "APTO" not in out and out.startswith("KR 60A # 11B-31")


def test_canonicalize_collapses_directional():
    assert canonicalize_for_match("AV 5 NORTE # 22-40, Versalles") == "AV 5N # 22-40"


def test_canonicalize_ordinal_to_digit():
    # "4TA" (cuarta) is the same avenue as "4"
    assert canonicalize_for_match("AV 4TA NORTE # 7N-53").startswith("AV 4N # 7N-53")
    assert canonicalize_for_match("CL 1RA # 2-3").startswith("CL 1 # 2-3")


def test_canonicalize_letter_suffix_is_not_ordinal():
    # "4A" is a sub-block subdivision, NOT an ordinal — must be preserved
    assert "4A" in canonicalize_for_match("KR 4A # 5-6")


def test_barrio_ok_missing_side_passes():
    assert barrio_ok("El Lido", "") is True
    assert barrio_ok("", "El Lido") is True


def test_barrio_ok_disagreement_fails():
    assert barrio_ok("El Lido", "Ciudad Jardin") is False


def test_coherent_rejects_neighbouring_blocks():
    # CL 8 # 38-120 vs CL 8 # 39-120 differ only in the cross number 38 vs 39
    v1 = address_to_vector("CL 8 # 38-120")
    v2 = address_to_vector("CL 8 # 39-120")
    assert coherent(v1, v2, strict=True) is False


def test_coherent_accepts_same_numbers():
    v1 = address_to_vector("CL 8 # 38-120")
    v2 = address_to_vector("CL 8 # 38-15")   # same street/cross, different placa
    assert coherent(v1, v2, strict=True) is True


def test_corner_key_order_independent():
    a = "KR 72 CON CL 11"
    b = "CL 11 # 72-15"
    ka = corner_key(a, address_to_vector(a))
    kb = corner_key(b, address_to_vector(b))
    assert ka is not None and ka == kb
