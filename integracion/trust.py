"""
Per-match trust score + reliability cutoff. Faithful port of EDA.ipynb 6.4,
with one addition: spatial-bridge matches made in SURROGATE mode (no official
parcel layer, only hand-entered coordinates) are discounted, honoring that
volunteer coordinates may not correspond to the official cadastre.
"""
from __future__ import annotations

import pandas as pd
from rapidfuzz import fuzz

from .config import METHOD_TRUST_BASE, TRUST_MIN
from .coords import haversine_m, parse_latlon
from .embedding import _EMB_EMPTY


def compute_trust(match_table: pd.DataFrame, emb, df_edan, df_visitas,
                  surrogate_bridge_vids: set | None = None) -> pd.DataFrame:
    """Adds a `trust` column, then rejects matches below TRUST_MIN (reverting
    them to unmatched). Returns the updated match_table.

    Signals (each applies only when both sides carry the datum):
      - semantic (LM cosine): >=.85 +.05 · >=.70 +.02 · <.45 -.10
      - barrio: agrees +.03 · contradicts -.08
      - geo: <=60m +.05 · >250m -.12
      - surrogate spatial-bridge: -.10 (coordinate-only, volunteer-entered)
    """
    E = df_edan.reset_index(drop=True)
    V = df_visitas.reset_index(drop=True)
    e_geo = [parse_latlon(x) for x in E["coords"]]
    v_geo = [parse_latlon(x) for x in V["coords"]]
    surrogate_bridge_vids = surrogate_bridge_vids or set()

    def match_trust(visita_id, sitio_id, method) -> float:
        j, i = emb.vid_pos[visita_id], emb.sid_pos[sitio_id]
        t = METHOD_TRUST_BASE[method]

        sim = float(emb.V_emb[j] @ emb.E_emb[i])
        if sim >= 0.85:
            t += 0.05
        elif sim >= 0.70:
            t += 0.02
        elif sim < 0.45:
            t -= 0.10

        b1 = str(V["barrio_vereda"].iloc[j]).strip()
        b2 = str(E["barrio_vereda"].iloc[i]).strip()
        if b1 not in _EMB_EMPTY and b2 not in _EMB_EMPTY:
            t += 0.03 if fuzz.token_sort_ratio(b1.upper(), b2.upper()) >= 75 else -0.08

        if v_geo[j] and e_geo[i]:
            dm = haversine_m(v_geo[j], e_geo[i])
            if dm <= 60:
                t += 0.05
            elif dm > 250:
                t -= 0.12

        if method == "spatial_bridge" and visita_id in surrogate_bridge_vids:
            t -= 0.10   # coordinate-only surrogate on volunteer-entered data

        return round(min(max(t, 0.05), 0.99), 2)

    match_table = match_table.copy()
    match_table["trust"] = [
        match_trust(r["visita_id"], r["sitio_id"], r["match_method"])
        if pd.notna(r["sitio_id"]) else None
        for _, r in match_table.iterrows()
    ]

    reject = match_table["trust"].notna() & (match_table["trust"] < TRUST_MIN)
    if reject.any():
        match_table.loc[reject, ["sitio_id", "match_method", "match_score", "trust"]] = \
            [None, None, None, None]
    return match_table
