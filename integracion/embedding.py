"""
LM semantic tier (model2vec static embeddings) — faithful port of EDA.ipynb 6.3.

Rescues pairs that no structural tier resolved, guarded by a reinforced numeric
guard (the LM understands language, not cadastral numbering).
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from .config import EMBEDDING_MIN_SIM
from .matching import barrio_ok, canonicalize_for_match
from .normalization import address_to_vector

_EMB_EMPTY = {"", "-", "nan", "None", "Na", "NA"}

_INTERP_ROAD = {
    "CL": "CALLE", "KR": "CARRERA", "AV": "AVENIDA", "AC": "AVENIDA CALLE",
    "AK": "AVENIDA CARRERA", "DG": "DIAGONAL", "TV": "TRANSVERSAL",
    "CQ": "CIRCUNVALAR", "AUT": "AUTOPISTA",
}
_INTERP_DIR = {"N": "NORTE", "S": "SUR", "E": "ESTE", "W": "OESTE", "O": "OESTE"}
_GLUED_DIR_RE = re.compile(r"(\d+[A-Z]?)([NSEWO])$")


def interpret_address(canon) -> str:
    """Rewrite a canonical cadastral address as natural Spanish for the LM."""
    s = str(canon).strip()
    if not s:
        return s
    toks = s.replace("#", " NUMERO ").replace("-", " ").split()
    out = []
    for t in toks:
        if t in _INTERP_ROAD:
            out.append(_INTERP_ROAD[t])
        elif t in _INTERP_DIR:
            out.append(_INTERP_DIR[t])
        else:
            m = _GLUED_DIR_RE.fullmatch(t)
            if m and m.group(2) in _INTERP_DIR:
                out.append(m.group(1))
                out.append(_INTERP_DIR[m.group(2)])
            else:
                out.append(t)
    return " ".join(out)


def _rec_text(canon, barrio, extra):
    parts = [interpret_address(canon)]
    b = str(barrio).strip()
    if b not in _EMB_EMPTY:
        parts.append(b.title())
    x = str(extra).strip()
    if x not in _EMB_EMPTY:
        parts.append(x[:80])
    return ", ".join(p for p in parts if p)


def _normalize_rows(X):
    return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)


def _part_compat(a, b, tol=0.001):
    if abs(a - b) <= tol:
        return True
    ia, ib = int(a), int(b)
    if ia != ib:
        return False
    return round(a - ia, 2) == 0 or round(b - ib, 2) == 0


def _text_nums(canon):
    return {int(n) for n in re.findall(r"\d+", str(canon))}


def embedding_guard(v, e, canon_v, canon_e, placa_tol=40.0):
    """Street-level compatibility guard for embedding matches."""
    v_parsed = v[0] != 0 and v[1] != 0
    e_parsed = e[0] != 0 and e[1] != 0
    if v_parsed and e_parsed:
        if int(v[0] // 1000) != int(e[0] // 1000):
            return False
        if not _part_compat(v[0] % 1000, e[0] % 1000):
            return False
        if not _part_compat(v[1], e[1]):
            return False
        if v[2] and e[2] and abs(v[2] - e[2]) > placa_tol:
            return False
        return True
    n1, n2 = _text_nums(canon_v), _text_nums(canon_e)
    if n1 and n2:
        return len(n1 & n2) >= min(2, len(n1), len(n2))
    return True


class EmbeddingIndex:
    """Encapsulates the model2vec encodings so trust.py can reuse E_emb/V_emb."""

    def __init__(self, df_edan, df_visitas):
        from model2vec import StaticModel

        self.E = df_edan.reset_index(drop=True)
        self.V = df_visitas.reset_index(drop=True)
        self.sid_pos = {s: i for i, s in enumerate(self.E["sitio_id"])}
        self.vid_pos = {v: i for i, v in enumerate(self.V["visita_id"])}

        self.e_canon = self.E["direccion_norm"].apply(canonicalize_for_match)
        self.v_canon = self.V["direccion_norm"].apply(canonicalize_for_match)
        self.e_addr_vecs = np.array([address_to_vector(a) for a in self.e_canon], dtype=np.float64)
        self.v_addr_vecs = np.array([address_to_vector(a) for a in self.v_canon], dtype=np.float64)

        e_rec = [_rec_text(self.e_canon.iloc[i], self.E["barrio_vereda"].iloc[i],
                           self.E.get("punto_referencia", pd.Series([""] * len(self.E))).iloc[i])
                 for i in range(len(self.E))]
        v_rec = [_rec_text(self.v_canon.iloc[i], self.V["barrio_vereda"].iloc[i],
                           self.V.get("nombre_estructura", pd.Series([""] * len(self.V))).iloc[i])
                 for i in range(len(self.V))]

        model = StaticModel.from_pretrained("minishlab/potion-multilingual-128M")
        self.E_emb = _normalize_rows(model.encode(e_rec))
        self.V_emb = _normalize_rows(model.encode(v_rec))

    def add_embedding_matches(self, match_table, min_sim: float = EMBEDDING_MIN_SIM, top_k: int = 5):
        out = match_table.copy().set_index("visita_id")
        unmatched = list(out.index[out["sitio_id"].isna()])
        if not unmatched:
            return out.reset_index()
        rows_pos = [self.vid_pos[v] for v in unmatched]
        S = self.V_emb[rows_pos] @ self.E_emb.T
        added = 0
        for k, j in enumerate(rows_pos):
            has_text = bool(self.v_canon.iloc[j].strip()) or \
                str(self.V.get("nombre_estructura", pd.Series([""] * len(self.V))).iloc[j]).strip() not in _EMB_EMPTY
            if not has_text:
                continue
            for i in np.argsort(-S[k])[:top_k]:
                sim = float(S[k][i])
                if sim < min_sim:
                    break
                if (embedding_guard(self.v_addr_vecs[j], self.e_addr_vecs[int(i)],
                                    self.v_canon.iloc[j], self.e_canon.iloc[int(i)])
                        and barrio_ok(self.V["barrio_vereda"].iloc[j],
                                      self.E["barrio_vereda"].iloc[int(i)])):
                    out.loc[unmatched[k], ["sitio_id", "match_method", "match_score"]] = (
                        self.E["sitio_id"].iloc[int(i)], "embedding", round(sim, 3))
                    added += 1
                    break
        print(f"embedding tier: +{added} matches")
        return out.reset_index()
