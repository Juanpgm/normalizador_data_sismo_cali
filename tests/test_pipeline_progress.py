"""
Progress / regression tests over the real integration.

Invariants (must always hold, no matter the data snapshot):
  - every accepted match has trust >= TRUST_MIN (the reliability cutoff)
  - each visita is matched to at most one sitio
  - the integrated table conserves every source record (outer merge)
  - the deliverable carries sitio_id, visita_id and trust_score
  - registro_id is unique

Regression floors (tunable, kept conservative to avoid flakiness): guard against
a silent collapse of the match rate.
"""
from integracion.config import TRUST_MIN

# Conservative floors — a real run should stay comfortably above these.
MIN_MATCH_RATE_PCT = 45.0
MIN_MATCHED = 350


def test_no_accepted_match_below_cutoff(artifacts):
    mt = artifacts["match_table"]
    accepted = mt[mt["sitio_id"].notna()]
    assert (accepted["trust"] >= TRUST_MIN).all(), \
        "un match aceptado quedó por debajo del corte de confiabilidad"


def test_each_visita_at_most_one_sitio(artifacts):
    mt = artifacts["match_table"]
    assert mt["visita_id"].is_unique, "una visita aparece emparejada más de una vez"


def test_match_rate_floor(artifacts):
    m = artifacts["report"]["matching"]
    assert m["visitas_emparejadas"] >= MIN_MATCHED, \
        f"solo {m['visitas_emparejadas']} matches (< {MIN_MATCHED})"
    assert m["tasa_match_pct"] >= MIN_MATCH_RATE_PCT, \
        f"tasa {m['tasa_match_pct']}% (< {MIN_MATCH_RATE_PCT}%)"


def test_integration_conserves_records(artifacts):
    df_edan = artifacts["df_edan"]
    df_visitas = artifacts["df_visitas"]
    df_integrado = artifacts["df_integrado"]
    counts = df_integrado["fuente"].value_counts()
    matched_rows = df_integrado[df_integrado["fuente"] == "edan+visita"]
    # Many visitas can point to the same sitio (repeat visits), so conservation
    # is checked on DISTINCT sitios and on visita rows separately:
    distinct_sitios_matched = matched_rows["sitio_id"].nunique()
    assert counts.get("solo_edan", 0) + distinct_sitios_matched == len(df_edan)
    assert counts.get("solo_visita", 0) + len(matched_rows) == len(df_visitas)


def test_registro_id_unique(artifacts):
    assert artifacts["df_integrado"].index.is_unique


def test_deliverable_has_keys_and_score(artifacts):
    cons = artifacts["df_consolidada"]
    for col in ("sitio_id", "visita_id", "trust_score"):
        assert col in cons.columns, f"falta la columna {col} en la tabla consolidada"


def test_deliverable_keeps_unmatched_records(artifacts):
    """The published table is the FULL result, not just the successful matches:
    unmatched EDAN sites and unmatched visitas must both survive. Filtering by
    trust belongs to df_confiable, never to the deliverable."""
    cons = artifacts["df_consolidada"]
    assert len(cons) == len(artifacts["df_integrado"])
    fuentes = set(cons["fuente"].unique())
    assert {"edan+visita", "solo_edan", "solo_visita"} <= fuentes
    assert (cons["sitio_id"].isna() | (cons["sitio_id"].astype(str) == "")).any()
    assert (cons["visita_id"].isna() | (cons["visita_id"].astype(str) == "")).any()


def test_spatial_bridge_ran(artifacts):
    info = artifacts["bridge_info"]
    assert info is not None
    assert info["mode"] in ("parcel", "surrogate")
