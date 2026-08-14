"""
Matching-only canonicalization, precision guards, and the confidence-ordered
cascade (tiers 1-7). Faithful port of EDA.ipynb sections 3.4, 6.1 and 6.2.

The spatial-bridge tier and the LM embedding tier live in their own modules
(spatial_bridge.py, embedding.py) and are stitched in by pipeline.py.
"""
from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

from .config import (
    BLOCK_PLACA_TOL, FUZZY_MIN_LEN, FUZZY_THRESHOLD, GEO_FAR_M, GEO_NEAR_M,
    TFIDF_MIN, VECTOR_TOL,
)
from .coords import haversine_m, parse_latlon
from .normalization import address_to_vector, make_handshake

# ── Matching-only canonicalization ────────────────────────────────────────────
_UNIT_NOISE_RE = re.compile(
    r'\b(APTO?|APARTAMENTO|APT|TORRE|CASA|BLOQUE|BLQ|BQ|PISO|LOCAL|OFICINA|OFC|'
    r'ETAPA|INTERIOR|INT|UNIDAD|CONJUNTO|EDIFICIO|EDIF|URBANIZACION|URB)\.?\s*[\w-]*',
    re.IGNORECASE,
)
_DIRECTIONALS = [
    (re.compile(r'\bNORTE\b|\bNTE\.?\b'), 'N'),
    (re.compile(r'\bOESTE\b|\bOE\.?\b'), 'O'),
    (re.compile(r'\bESTE\b'), 'E'),
    (re.compile(r'\bSUR\b'), 'S'),
]
_GLUED_ROAD_RE = re.compile(
    r'\b(CALLE|CARRERA|AVENIDA|DIAGONAL|TRANSVERSAL|CLL|CRA|KRA|CL|KR|AV|DG|TV)(\d)',
    re.IGNORECASE,
)
# Spanish ordinal spellings of street numbers → plain digit. "4TA"=cuarta,
# "1RA"=primera, "7MA"=séptima, … These are the SAME street as the digit, so
# collapsing them is a safe, unambiguous recall gain (never touches letter
# suffixes like "4A", which are a distinct sub-block subdivision).
_ORDINAL_RE = re.compile(r'\b(\d+)(?:RA|ERA|DA|TA|MA|VA|NA)\b', re.IGNORECASE)
_ROAD_WORDS = [
    (re.compile(r'\b(CALLE|CLL)\b'), 'CL'),
    (re.compile(r'\b(CARRERA|CRA|KRA|CR)\b'), 'KR'),
    (re.compile(r'\bAVENIDA\b'), 'AV'),
    (re.compile(r'\bDIAGONAL\b'), 'DG'),
    (re.compile(r'\bTRANSVERSAL\b'), 'TV'),
]


def canonicalize_for_match(addr) -> str:
    """Matching-only cleanup on top of normalize_address output.

    "KR 60A # 11B-31 APTO 203B, Santa Anita, Cali" → "KR 60A # 11B-31"
    "AV 5 NORTE # 22-40, Versalles, Cali"          → "AV 5N # 22-40"
    """
    if not addr or str(addr).strip() in {"", "-"}:
        return ""
    s = str(addr).split(",")[0].strip().upper()
    s = _ORDINAL_RE.sub(r'\1', s)          # 4TA → 4, 1RA → 1 (same street)
    s = _GLUED_ROAD_RE.sub(lambda m: m.group(1) + " " + m.group(2), s)
    for rx, rep in _ROAD_WORDS:
        s = rx.sub(rep, s)
    s = _UNIT_NOISE_RE.sub(" ", s)
    for rx, rep in _DIRECTIONALS:
        s = rx.sub(rep, s)
    s = re.sub(r'(\d)\s+([NOSE])\b', r'\1\2', s)
    return re.sub(r'\s+', ' ', s).strip()


# ── Precision guards ──────────────────────────────────────────────────────────
def barrio_ok(b1, b2, threshold: int = 75) -> bool:
    """True if either barrio is missing OR both fuzzy-agree (token_sort >= thr)."""
    b1, b2 = str(b1).strip(), str(b2).strip()
    if b1 in {"", "-", "nan", "None"} or b2 in {"", "-", "nan", "None"}:
        return True
    return fuzz.token_sort_ratio(b1.upper(), b2.upper()) >= threshold


def road_nums(vec):
    """Integer street numbers present in a parsed address vector: {road, cross}."""
    return {n for n in (int(vec[0] % 1000), int(vec[1])) if n != 0}


def coherent(v1, v2, strict: bool = True) -> bool:
    """Numeric guard for text-similarity matches: street numbers must agree."""
    n1, n2 = road_nums(v1), road_nums(v2)
    if not n1 or not n2:
        return True
    if strict:
        return n1 == n2 or n1 <= n2 or n2 <= n1
    return bool(n1 & n2)


def corner_key(canon, vec):
    """Order-independent corner fingerprint (sorted pair of both street numbers)."""
    if not canon or vec[0] == 0:
        return None
    rn = round(float(vec[0]) % 1000, 2)
    cn = round(float(vec[1]), 2)
    if cn == 0:
        m = re.search(
            r'\b(?:CON|X|ESQUINA)\s+(?:AU|AV|AC|AK|KR|CL|CV|CQ|CT|DG|TV|TC)?\s*(\d+)\s*([A-Z]?)\b',
            canon,
        )
        if m:
            cn = round(float(m.group(1)) + ((ord(m.group(2)) - 64) * 0.01 if m.group(2) else 0), 2)
    return tuple(sorted((rn, cn))) if cn else None


# ── Confidence cascade (tiers 1-7) ────────────────────────────────────────────
def build_match_table(df_edan, df_visitas,
                      vector_tol: float = VECTOR_TOL,
                      block_placa_tol: float = BLOCK_PLACA_TOL,
                      geo_near_m: float = GEO_NEAR_M, geo_far_m: float = GEO_FAR_M,
                      tfidf_min: float = TFIDF_MIN,
                      fuzzy_threshold: float = FUZZY_THRESHOLD,
                      fuzzy_min_len: int = FUZZY_MIN_LEN) -> pd.DataFrame:
    """7-tier cascade ordered by confidence, first hit wins.

    Tiers: handshake · vector · vector_block · corner · geo · tfidf · fuzzy.
    Returns one row per visita: visita_id | sitio_id | match_method | match_score.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    E = df_edan.reset_index(drop=True)
    V = df_visitas.reset_index(drop=True)

    e_canon = E["direccion_norm"].apply(canonicalize_for_match)
    v_canon = V["direccion_norm"].apply(canonicalize_for_match)
    e_vecs = np.array([address_to_vector(a) for a in e_canon], dtype=np.float64)
    v_vecs = np.array([address_to_vector(a) for a in v_canon], dtype=np.float64)
    e_sids = E["sitio_id"].to_numpy()
    e_barrio = E["barrio_vereda"].astype(str).to_numpy()
    v_barrio = V["barrio_vereda"].astype(str).to_numpy()

    # Tier 1 index: strong canonical fingerprints only (must contain '#')
    hs_index = {}
    for i, a in enumerate(e_canon):
        k = make_handshake(a)
        if k and "#" in k and k not in hs_index:
            hs_index[k] = i

    has_cross = e_vecs[:, 1] != 0.0

    # Tier 4 index: corner fingerprint -> EDAN row indices
    corner_index = {}
    for i in range(len(E)):
        ck = corner_key(e_canon.iloc[i], e_vecs[i])
        if ck:
            corner_index.setdefault(ck, []).append(i)

    # Tier 5 pool: parseable WGS84 coordinates
    e_geo = [parse_latlon(x) for x in E["coords"]]
    v_geo = [parse_latlon(x) for x in V["coords"]]
    e_geo_idx = [i for i, g in enumerate(e_geo) if g]

    # Tier 6/7 pool: canonical address + barrio as similarity text
    def txt(canon, barrio):
        b = str(barrio).strip()
        return (canon + " " + b.upper()) if b not in {"", "-", "nan"} else canon

    e_text_idx = [i for i in range(len(E)) if e_canon.iloc[i].strip()]
    e_texts = [txt(e_canon.iloc[i], e_barrio[i]) for i in e_text_idx]
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))
    Xe = vectorizer.fit_transform(e_texts)

    rows = []
    for j in range(len(V)):
        addr, vv = v_canon.iloc[j], v_vecs[j]
        sid = method = score = None

        # 1. handshake
        if addr:
            k = make_handshake(addr)
            if k and "#" in k and k in hs_index:
                sid, method, score = e_sids[hs_index[k]], "handshake", 100.0

        # 2. vector (exact sub-block)
        if sid is None and vv[1] != 0.0:
            pool = np.where(has_cross)[0]
            d = np.linalg.norm(e_vecs[pool] - vv, axis=1)
            b = int(np.argmin(d))
            if d[b] <= vector_tol:
                sid, method, score = e_sids[pool[b]], "vector", round(float(d[b]), 4)

        # 3. vector_block (same block face, different placa)
        if sid is None and vv[1] != 0.0:
            pool = np.where(has_cross)[0]
            d01 = np.linalg.norm(e_vecs[pool][:, :2] - vv[:2], axis=1)
            cand = pool[d01 <= 0.001]
            if len(cand):
                placa_d = np.abs(e_vecs[cand][:, 2] - vv[2])
                for oi in np.argsort(placa_d):
                    i = cand[oi]
                    if placa_d[oi] <= block_placa_tol and barrio_ok(v_barrio[j], e_barrio[i]):
                        sid, method, score = e_sids[i], "vector_block", round(float(placa_d[oi]), 1)
                        break

        # 4. corner
        if sid is None and addr:
            ck = corner_key(addr, vv)
            if ck and ck in corner_index:
                for i in corner_index[ck]:
                    if barrio_ok(v_barrio[j], e_barrio[i]):
                        sid, method, score = e_sids[i], "corner", 100.0
                        break

        # 5. geo
        if sid is None and v_geo[j] and e_geo_idx:
            dists = sorted((haversine_m(v_geo[j], e_geo[i]), i) for i in e_geo_idx)
            for dm, i in dists[:3]:
                edan_unparseable = e_vecs[i][0] == 0
                limit = geo_far_m if edan_unparseable else geo_near_m
                if dm <= limit and coherent(vv, e_vecs[i], strict=False):
                    sid, method, score = e_sids[i], "geo", round(dm, 1)
                    break

        # 6. tfidf (ML text similarity, guarded)
        if sid is None and addr:
            q = vectorizer.transform([txt(addr, v_barrio[j])])
            sims = cosine_similarity(q, Xe).ravel()
            for b in np.argsort(-sims)[:5]:
                if sims[b] < tfidf_min:
                    break
                i = e_text_idx[int(b)]
                if coherent(vv, e_vecs[i], strict=True) and barrio_ok(v_barrio[j], e_barrio[i]):
                    sid, method, score = e_sids[i], "tfidf", round(float(sims[b]), 3)
                    break

        # 7. fuzzy (guarded, only for specific-enough addresses)
        if sid is None and addr and len(addr) >= fuzzy_min_len:
            hits = process.extract(addr, e_texts, scorer=fuzz.token_set_ratio,
                                   score_cutoff=fuzzy_threshold, limit=5)
            for _, sc, hi in hits:
                i = e_text_idx[hi]
                if coherent(vv, e_vecs[i], strict=True) and barrio_ok(v_barrio[j], e_barrio[i]):
                    sid, method, score = e_sids[i], "fuzzy", round(sc, 1)
                    break

        rows.append({"visita_id": V["visita_id"].iloc[j], "sitio_id": sid,
                     "match_method": method, "match_score": score})

    return pd.DataFrame(rows)
