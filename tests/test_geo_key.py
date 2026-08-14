"""The two new tiers: the declared pre-registration key and the last-resort
coordinate key, whose whole job is to refuse ambiguous matches."""
import pandas as pd

from integracion.matching import (add_geo_key_matches, build_preregistro_index,
                                  clean_consecutivo)


# ── Pre-registration ──────────────────────────────────────────────────────────
def test_clean_consecutivo_rejects_the_prose():
    """Volunteers answer this field freely; only a plain number is a key."""
    for junk in ("No", "No tengo", "Fue asignado", "N.A", "0", "00", "13-33", ""):
        assert clean_consecutivo(junk) == ""
    assert clean_consecutivo(" 563 ") == "563"
    assert clean_consecutivo("1") == "1"


def test_build_preregistro_index_drops_duplicates():
    """A consecutive pointing at two EDAN rows identifies neither."""
    edan = pd.DataFrame({"consecutivo_edan": ["10", "20", "20", "no", "0"]})
    index = build_preregistro_index(edan)
    assert index == {"10": 0}


def test_build_preregistro_index_without_column():
    assert build_preregistro_index(pd.DataFrame({"sitio_id": ["A"]})) == {}


# ── Geo key ───────────────────────────────────────────────────────────────────
def _edan(rows, fuentes=None):
    df = pd.DataFrame(rows, columns=["sitio_id", "coords", "barrio_vereda"])
    if fuentes is not None:
        df["coords_fuente"] = fuentes
    return df


def _visitas(rows):
    return pd.DataFrame(rows, columns=["visita_id", "coords", "barrio_vereda",
                                       "coords_fuente"])


def _table(visita_ids, matched=None):
    matched = matched or {}
    return pd.DataFrame({
        "visita_id": visita_ids,
        "sitio_id": [matched.get(v) for v in visita_ids],
        "match_method": [None] * len(visita_ids),
        "match_score": [None] * len(visita_ids),
        "trust": [None] * len(visita_ids),
    })


def test_geo_key_matches_an_isolated_nearby_site():
    edan = _edan([["S1", "3.440000, -76.530000", "Centro"],
                  ["S2", "3.460000, -76.550000", "Centro"]])   # ~3 km away
    visitas = _visitas([["V1", "3.440050, -76.530050", "Centro", "exif"]])
    out, info = add_geo_key_matches(_table(["V1"]), edan, visitas)
    assert out.at[0, "sitio_id"] == "S1"
    assert out.at[0, "match_method"] == "geo_key"
    assert info["added"] == 1
    # a measured coordinate is trusted slightly above the tier's base
    assert out.at[0, "trust"] > 0.74


def test_geo_key_refuses_when_two_sites_are_equally_close():
    """The guard that matters: proximity without isolation is not a key."""
    edan = _edan([["S1", "3.440000, -76.530000", "Centro"],
                  ["S2", "3.440090, -76.530000", "Centro"]])   # ~10 m apart
    visitas = _visitas([["V1", "3.440040, -76.530000", "Centro", "exif"]])
    out, info = add_geo_key_matches(_table(["V1"]), edan, visitas)
    assert out.at[0, "sitio_id"] is None
    assert info["rechazadas_ambiguas"] == 1
    assert info["added"] == 0


def test_geo_key_refuses_beyond_the_radius():
    edan = _edan([["S1", "3.450000, -76.530000", "Centro"]])   # ~1.1 km away
    visitas = _visitas([["V1", "3.440000, -76.530000", "Centro", "exif"]])
    out, info = add_geo_key_matches(_table(["V1"]), edan, visitas)
    assert out.at[0, "sitio_id"] is None
    assert info["rechazadas_lejos"] == 1


def test_geo_key_never_touches_an_existing_match():
    edan = _edan([["S1", "3.440000, -76.530000", "Centro"]])
    visitas = _visitas([["V1", "3.440050, -76.530050", "Centro", "exif"]])
    table = _table(["V1"], matched={"V1": "S9"})
    out, info = add_geo_key_matches(table, edan, visitas)
    assert out.at[0, "sitio_id"] == "S9"
    assert info["candidatas"] == 0


def test_geo_key_skips_visitas_without_coordinates():
    edan = _edan([["S1", "3.440000, -76.530000", "Centro"]])
    visitas = _visitas([["V1", "no tengo", "Centro", ""]])
    out, info = add_geo_key_matches(_table(["V1"]), edan, visitas)
    assert out.at[0, "sitio_id"] is None
    assert info["candidatas"] == 0


# ── Geocoded coordinates: the anti-circularity guard ──────────────────────────
def test_geo_key_refuses_a_geocode_geocode_pair():
    """Two geocoded points close together = the addresses geocode to the same
    place. That is address matching in disguise — the text tiers' job."""
    edan = _edan([["S1", "3.440000, -76.530000", "Centro"]], fuentes=["geocode"])
    visitas = _visitas([["V1", "3.440050, -76.530050", "Centro", "geocode"]])
    out, info = add_geo_key_matches(_table(["V1"]), edan, visitas)
    assert out.at[0, "sitio_id"] is None
    assert info["rechazadas_circular"] == 1
    assert info["added"] == 0


def test_geo_key_allows_exif_against_geocoded_edan():
    """Instrument GPS vs a geocoded parcel — the pairing this was built for."""
    edan = _edan([["S1", "3.440000, -76.530000", "Centro"]], fuentes=["geocode"])
    visitas = _visitas([["V1", "3.440050, -76.530050", "Centro", "exif"]])
    out, info = add_geo_key_matches(_table(["V1"]), edan, visitas)
    assert out.at[0, "sitio_id"] == "S1"
    assert info["added"] == 1
    # base 0.74 + 0.04 exif - 0.05 geocoded side = 0.73, above the cutoff
    assert out.at[0, "trust"] == 0.73


def test_geo_key_drops_matches_below_the_trust_cutoff():
    """A typed visita coord against a geocoded EDAN lands under TRUST_MIN —
    the tier must refuse it rather than publish a sub-cutoff match."""
    edan = _edan([["S1", "3.440000, -76.530000", "Centro"]], fuentes=["geocode"])
    visitas = _visitas([["V1", "3.440050, -76.530050", "Centro", "visita"]])
    out, info = add_geo_key_matches(_table(["V1"]), edan, visitas)
    assert out.at[0, "sitio_id"] is None
    assert info["rechazadas_trust"] == 1
