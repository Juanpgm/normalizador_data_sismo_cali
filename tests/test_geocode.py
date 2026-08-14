"""Geocoding: address translation, response validation, idempotent cache,
fill-only application, and graceful degradation. No network — fake sessions."""
import json

import pandas as pd
import pytest

from integracion import geocode as gc


# ── Address translation ───────────────────────────────────────────────────────
def test_to_google_address_expands_road_abbreviations():
    out = gc.to_google_address("KR 42 # 5-25, Urbanización Tequendama, Cali")
    assert out == ("Carrera 42 # 5-25, Urbanización Tequendama, "
                   "Cali, Valle del Cauca, Colombia")


def test_to_google_address_strips_unit_noise_but_keeps_barrio():
    out = gc.to_google_address(
        "CL 1D OESTE 100 BIS 33 TORRE 85, Altos De Santa Helena, Cali")
    assert "TORRE" not in out.upper()
    # the barrio survives even though URBANIZACION-style words are unit noise
    assert "Altos De Santa Helena" in out
    assert out.startswith("Calle 1D OESTE 100 BIS 33")


def test_to_google_address_keeps_corners_and_urban_barrios():
    out = gc.to_google_address("KR 67 CON CL 3 C, Urbanización El Refugio, Cali")
    assert out.startswith("Carrera 67 CON Calle 3 C")
    assert "Urbanización El Refugio" in out      # barrio segment untouched


def test_to_google_address_empty_input():
    assert gc.to_google_address("") == ""
    assert gc.to_google_address("-") == ""


# ── Fake HTTP session ─────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, payload, status=200):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


def _google_payload(lat, lon, location_type, partial=False, status="OK"):
    return {"status": status, "results": [{
        "geometry": {"location": {"lat": lat, "lng": lon},
                     "location_type": location_type},
        "partial_match": partial,
        "formatted_address": "Cra 42 #5-25, Cali, Colombia",
        "place_id": "pid123",
    }] if status == "OK" else []}


class _Session:
    def __init__(self, payloads):
        self.payloads, self.calls = list(payloads), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return _Resp(self.payloads.pop(0))


# ── Validation of one response ────────────────────────────────────────────────
def test_geocode_one_accepts_rooftop_in_cali():
    s = _Session([_google_payload(3.44, -76.53, "ROOFTOP")])
    rec = gc.geocode_one("Carrera 42 # 5-25, Cali", s, "KEY")
    assert rec["accepted"] is True
    assert rec["lat"] == 3.44 and rec["lon"] == -76.53
    assert rec["precision_m"] == 15.0


def test_geocode_one_rejects_geometric_center():
    s = _Session([_google_payload(3.44, -76.53, "GEOMETRIC_CENTER")])
    rec = gc.geocode_one("x", s, "KEY")
    assert rec["accepted"] is False and rec["reason"] == "precision_insuficiente"


def test_geocode_one_rejects_outside_cali():
    s = _Session([_google_payload(4.60, -74.08, "ROOFTOP")])   # Bogotá
    rec = gc.geocode_one("x", s, "KEY")
    assert rec["accepted"] is False and rec["reason"] == "fuera_de_cali"


def test_geocode_one_records_partial_match_but_accepts():
    s = _Session([_google_payload(3.44, -76.53, "ROOFTOP", partial=True)])
    rec = gc.geocode_one("x", s, "KEY")
    assert rec["accepted"] is True and rec["partial"] is True


def test_geocode_one_caches_zero_results_as_rejection():
    s = _Session([_google_payload(0, 0, "", status="ZERO_RESULTS")])
    rec = gc.geocode_one("x", s, "KEY")
    assert rec["accepted"] is False and rec["reason"] == "sin_resultado"


def test_geocode_one_request_denied_raises():
    s = _Session([{"status": "REQUEST_DENIED", "results": [],
                   "error_message": "no billing"}])
    with pytest.raises(gc.GeocodeUnavailable):
        gc.geocode_one("x", s, "KEY")


# ── Cache: idempotence is the whole point ─────────────────────────────────────
def test_harvest_uses_cache_and_never_recalls(tmp_path, monkeypatch):
    monkeypatch.setenv(gc.CACHE_ENV, str(tmp_path))
    edan = pd.DataFrame({"sitio_id": ["S1"], "coords": [""],
                         "direccion_norm": ["KR 42 # 5-25, Tequendama, Cali"]})
    visitas = pd.DataFrame({"visita_id": [], "coords": [], "direccion_norm": []})

    s1 = _Session([_google_payload(3.44, -76.53, "ROOFTOP")])
    cache, info = gc.harvest_geocode(edan, visitas, session=s1, api_key="KEY")
    assert s1.calls == 1 and info["llamadas_api"] == 1

    # second run: same address → zero HTTP calls, answered from disk
    s2 = _Session([])
    cache2, info2 = gc.harvest_geocode(edan, visitas, session=s2, api_key="KEY")
    assert s2.calls == 0
    assert info2["cache_hits"] == 1 and info2["llamadas_api"] == 0
    key = gc.cache_key("KR 42 # 5-25, Tequendama, Cali")
    assert cache2[key]["accepted"] is True


def test_rejections_are_cached_too(tmp_path, monkeypatch):
    monkeypatch.setenv(gc.CACHE_ENV, str(tmp_path))
    edan = pd.DataFrame({"sitio_id": ["S1"], "coords": [""],
                         "direccion_norm": ["CL FALSA 123, Cali"]})
    visitas = pd.DataFrame({"visita_id": [], "coords": [], "direccion_norm": []})
    s1 = _Session([_google_payload(0, 0, "", status="ZERO_RESULTS")])
    gc.harvest_geocode(edan, visitas, session=s1, api_key="KEY")
    s2 = _Session([])
    _, info = gc.harvest_geocode(edan, visitas, session=s2, api_key="KEY")
    assert s2.calls == 0 and info["cache_hits"] == 1


def test_cache_merge_newest_wins(tmp_path):
    old = {"k": {"accepted": False, "reason": "sin_resultado", "ts": 100}}
    new = {"k": {"accepted": True, "lat": 3.4, "lon": -76.5, "ts": 200}}
    d1, d2 = tmp_path / "a", tmp_path / "b"
    for d, payload in ((d1, old), (d2, new)):
        d.mkdir()
        (d / gc.CACHE_FILE).write_text(json.dumps(payload), encoding="utf-8")
    merged = gc.load_cache([d1, d2])
    assert merged["k"]["accepted"] is True


def test_geocode_limit_caps_api_spend(tmp_path, monkeypatch):
    monkeypatch.setenv(gc.CACHE_ENV, str(tmp_path))
    edan = pd.DataFrame({
        "sitio_id": ["S1", "S2", "S3"], "coords": ["", "", ""],
        "direccion_norm": [f"KR {n} # 5-25, Centro, Cali" for n in (10, 11, 12)]})
    visitas = pd.DataFrame({"visita_id": [], "coords": [], "direccion_norm": []})
    s = _Session([_google_payload(3.44, -76.53, "ROOFTOP")] * 3)
    _, info = gc.harvest_geocode(edan, visitas, session=s, api_key="KEY", limit=2)
    assert s.calls == 2 and info["llamadas_api"] == 2


# ── Applying the harvest: fill-only ───────────────────────────────────────────
def _cache_for(addr, lat=3.44, lon=-76.53):
    return {gc.cache_key(addr): {"accepted": True, "lat": lat, "lon": lon,
                                 "location_type": "ROOFTOP", "precision_m": 15.0,
                                 "ts": 1}}


def test_apply_geocode_fills_only_empty_coords():
    df = pd.DataFrame({
        "sitio_id": ["S1", "S2"],
        "coords": ["", "3.400000, -76.500000"],
        "direccion_norm": ["KR 1 # 2-3, Centro, Cali", "KR 9 # 9-9, Centro, Cali"],
    })
    cache = {**_cache_for("KR 1 # 2-3, Centro, Cali"),
             **_cache_for("KR 9 # 9-9, Centro, Cali", lat=3.99, lon=-76.99)}
    out = gc.apply_geocode(df, cache, typed_label="edan")
    assert out["coords"].iloc[0] == "3.440000, -76.530000"
    assert out["coords_fuente"].iloc[0] == "geocode"
    assert out["coords_precision_m"].iloc[0] == 15.0
    # the existing coordinate was NOT touched, and got its typed label
    assert out["coords"].iloc[1] == "3.400000, -76.500000"
    assert out["coords_fuente"].iloc[1] == "edan"


def test_apply_geocode_ignores_rejected_cache_entries():
    df = pd.DataFrame({"sitio_id": ["S1"], "coords": [""],
                       "direccion_norm": ["CL FALSA 123, Cali"]})
    cache = {gc.cache_key("CL FALSA 123, Cali"):
             {"accepted": False, "reason": "sin_resultado", "ts": 1}}
    out = gc.apply_geocode(df, cache, typed_label="edan")
    assert out["coords"].iloc[0] == ""
    assert out["coords_fuente"].iloc[0] == ""


def test_harvest_without_api_key_degrades(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(gc.CACHE_ENV, str(tmp_path))
    monkeypatch.delenv(gc.API_KEY_ENV, raising=False)
    edan = pd.DataFrame({"sitio_id": ["S1"], "coords": [""],
                         "direccion_norm": ["KR 1 # 2-3, Centro, Cali"]})
    visitas = pd.DataFrame({"visita_id": [], "coords": [], "direccion_norm": []})
    cache, info = gc.harvest_geocode(edan, visitas, session=_Session([]))
    assert info["llamadas_api"] == 0
    assert "GOOGLE_MAPS_API_KEY" in capsys.readouterr().out
