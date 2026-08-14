"""
EXPERIMENT (not a production tier): supervised pairwise re-ranker to probe
whether ANY additional SAFE matches exist beyond the cascade's 653.

Method (from the address-ER research: blocking + learned classifier + hard
negatives, Ranks 2/3/4):
  1. Block candidate (visita, edan) pairs by barrio agreement (subtractive, safe).
  2. Silver POSITIVES = the cascade's exact/near-exact tiers
     (handshake, vector, vector_block, corner) — near-certain same-site pairs.
  3. HARD NEGATIVES = same-barrio EDAN candidates that are NOT the matched site
     for a matched visita (the Cali-grid neighbours: CL 8 #38 vs CL 8 #39).
     Plus easy cross-barrio negatives.
  4. Features per pair: char-TFIDF cosine, Jaro-Winkler, token_set/sort,
     normalized Damerau-Levenshtein, model2vec cosine, road/cross/placa deltas,
     number-set Jaccard, barrio agreement, geo distance flag, same-road-type.
  5. Train HistGradientBoostingClassifier, evaluate with GroupKFold grouped by
     visita (no leakage), report precision at a precision-first threshold.
  6. Apply to still-UNMATCHED visitas' candidates; accept only p >= 0.97 that
     ALSO pass the hard guards (strict numeric coherence + barrio). Print each
     new pair for manual verification.

Run: python -m integracion.experiments.ml_reranker
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from rapidfuzz import fuzz
from rapidfuzz.distance import DamerauLevenshtein, JaroWinkler
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import precision_recall_fscore_support
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.model_selection import GroupKFold

from ..coords import haversine_m, parse_latlon
from ..embedding import EmbeddingIndex
from ..matching import (address_to_vector, barrio_ok, build_match_table,
                        canonicalize_for_match, coherent)
from ..pipeline import load_data

EXACT_TIERS = {"handshake", "vector", "vector_block", "corner"}


def _nums(vec):
    return {int(vec[0] % 1000), int(vec[1]), int(vec[2])} - {0}


def _features(vc, ec, vv, ev, vb, eb, vg, eg, tfidf):
    nv, ne = _nums(vv), _nums(ev)
    jac = len(nv & ne) / max(len(nv | ne), 1) if (nv or ne) else 0.0
    geo = haversine_m(vg, eg) if (vg and eg) else -1.0
    return [
        tfidf,
        JaroWinkler.similarity(vc, ec),
        fuzz.token_set_ratio(vc, ec) / 100,
        fuzz.token_sort_ratio(vc, ec) / 100,
        DamerauLevenshtein.normalized_similarity(vc, ec),
        abs(vv[0] - ev[0]),
        abs(vv[1] - ev[1]),
        abs(vv[2] - ev[2]),
        jac,
        fuzz.token_sort_ratio(str(vb).upper(), str(eb).upper()) / 100,
        1.0 if int(vv[0] // 1000) == int(ev[0] // 1000) else 0.0,
        geo,
    ]


def run_experiment():
    e, v = load_data(use_cache=True)
    e = e.reset_index(drop=True); v = v.reset_index(drop=True)
    mt = build_match_table(e, v)

    e_canon = e["direccion_norm"].apply(canonicalize_for_match).tolist()
    v_canon = v["direccion_norm"].apply(canonicalize_for_match).tolist()
    e_vec = np.array([address_to_vector(a) for a in e_canon])
    v_vec = np.array([address_to_vector(a) for a in v_canon])
    e_bar = e["barrio_vereda"].astype(str).tolist()
    v_bar = v["barrio_vereda"].astype(str).tolist()
    e_geo = [parse_latlon(x) for x in e["coords"]]
    v_geo = [parse_latlon(x) for x in v["coords"]]
    e_sid = e["sitio_id"].tolist()
    sid_row = {s: i for i, s in enumerate(e_sid)}
    vid_row = {vi: j for j, vi in enumerate(v["visita_id"])}

    emb = EmbeddingIndex(e, v)

    def emb_cos(j, i):
        return float(emb.V_emb[j] @ emb.E_emb[i])

    # TF-IDF over EDAN canon+barrio, to score candidates and to block by top-k
    def txt(c, b):
        b = str(b).strip()
        return (c + " " + b.upper()) if b not in {"", "-", "nan"} else c
    e_text = [txt(e_canon[i], e_bar[i]) for i in range(len(e))]
    vzr = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))
    Xe = vzr.fit_transform(e_text)

    def candidates(j, top=20):
        q = vzr.transform([txt(v_canon[j], v_bar[j])])
        sims = cosine_similarity(q, Xe).ravel()
        order = np.argsort(-sims)[:top]
        return [(int(i), float(sims[i])) for i in order]

    # ── Build training set ────────────────────────────────────────────────────
    X, y, groups = [], [], []
    n_pos = n_hardneg = 0
    for _, r in mt[mt["sitio_id"].notna()].iterrows():
        if r["match_method"] not in EXACT_TIERS:
            continue
        j = vid_row[r["visita_id"]]; itrue = sid_row[r["sitio_id"]]
        cands = candidates(j)
        for i, tf in cands:
            lbl = 1 if i == itrue else 0
            if lbl == 0 and not barrio_ok(v_bar[j], e_bar[i]):
                continue  # keep hard negatives (same-barrio grid neighbours)
            X.append(_features(v_canon[j], e_canon[i], v_vec[j], e_vec[i],
                               v_bar[j], e_bar[i], v_geo[j], e_geo[i], tf) + [emb_cos(j, i)])
            y.append(lbl); groups.append(j)
            if lbl: n_pos += 1
            else: n_hardneg += 1
        if sid_row[r["sitio_id"]] not in [c[0] for c in cands]:
            # ensure the true positive is present even if outside top-k
            i = itrue
            X.append(_features(v_canon[j], e_canon[i], v_vec[j], e_vec[i],
                               v_bar[j], e_bar[i], v_geo[j], e_geo[i], 1.0) + [emb_cos(j, i)])
            y.append(1); groups.append(j); n_pos += 1

    X = np.array(X); y = np.array(y); groups = np.array(groups)
    print(f"Entrenamiento: {n_pos} positivos · {n_hardneg} negativos duros (mismo barrio)")

    # ── Cross-validated precision/recall grouped by visita ────────────────────
    gkf = GroupKFold(n_splits=5)
    P = R = F = 0.0
    for tr, te in gkf.split(X, y, groups):
        clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08,
                                             l2_regularization=1.0, random_state=0)
        clf.fit(X[tr], y[tr])
        proba = clf.predict_proba(X[te])[:, 1]
        pred = (proba >= 0.97).astype(int)
        p, r, f, _ = precision_recall_fscore_support(y[te], pred, average="binary",
                                                     zero_division=0)
        P += p; R += r; F += f
    print(f"CV (umbral p>=0.97, agrupado por visita): "
          f"precision={P/5:.3f} recall={R/5:.3f} F1={F/5:.3f}")

    # ── Fit on all, apply to UNMATCHED visitas ────────────────────────────────
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08,
                                         l2_regularization=1.0, random_state=0)
    clf.fit(X, y)
    unmatched = mt.loc[mt["sitio_id"].isna(), "visita_id"].tolist()
    added, examples = 0, []
    for vid in unmatched:
        j = vid_row[vid]
        if not v_canon[j].strip():
            continue
        best = None
        for i, tf in candidates(j):
            if not barrio_ok(v_bar[j], e_bar[i]):
                continue
            feat = np.array([_features(v_canon[j], e_canon[i], v_vec[j], e_vec[i],
                                       v_bar[j], e_bar[i], v_geo[j], e_geo[i], tf) + [emb_cos(j, i)]])
            p = float(clf.predict_proba(feat)[0, 1])
            # hard guard AFTER the model: strict numeric coherence must hold
            if p >= 0.97 and coherent(v_vec[j], e_vec[i], strict=True):
                if best is None or p > best[1]:
                    best = (i, p)
        if best is not None:
            added += 1
            i = best[0]
            if len(examples) < 30:
                examples.append((round(best[1], 3), v["direccion_norm"].iloc[j][:40],
                                 e["direccion_norm"].iloc[i][:40]))
    print(f"\n>>> Matches SEGUROS nuevos del re-ranker ML (p>=0.97 + guard estricto): {added}")
    for p, a, b in sorted(examples, reverse=True):
        print(f"  p={p} | {a:40s} | {b}")
    # feature importance proxy (permutation would be costly; report which features
    # the model splits on most via the built-in)
    return {"added": added, "n_pos": n_pos, "n_hardneg": n_hardneg}


if __name__ == "__main__":
    run_experiment()
