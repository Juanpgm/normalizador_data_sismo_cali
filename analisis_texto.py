"""Knowledge-graph-driven analysis of EDE free-text fields.

Loads `knowledge/kg.json` (curated from NSR-10 / AIS manual, see
knowledge/build_kg.py) and scores inspection comments against it:

    analizar(texto) -> {
        "hallazgos":     [(patologia_id, elemento_id | None, cita)],
        "acciones":      set of accion ids detected (e.g. {"demoler"}),
        "gravedad_texto": float 0-1,
    }

Method: sentence-level matching. A `termino` node's regex detects a
patologia / elemento / accion; a patologia co-occurring with an elemento in
the SAME sentence forms a paired finding whose weight is
peso(patologia -> criterio, edge `indica`) x criticidad(elemento, edge
`pondera`). An unpaired patologia counts at the base weight x the default
criticality. Sentences that negate ("no se requiere demolicion", "sin
grietas") are skipped for the negated concept. gravedad_texto is the capped
sum over distinct findings.

    python analisis_texto.py --check   # offline self-check on corpus-style text
"""
from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

KG_PATH = Path(__file__).resolve().parent / "knowledge" / "kg.json"

# Generic negation: "no|sin|no se (requiere|recomienda|presenta|observa|...)"
# shortly before the concept, within the same sentence.
NEG_RE = re.compile(
    r"\b(?:no|sin|ni|tampoco)\b(?:\s+se)?(?:\s+\w+){0,4}\s*$", re.IGNORECASE)
SENT_SPLIT = re.compile(r"[.;\n]+")
# Default criticality when a patologia appears with no element attached: the
# AIS manual scopes the form's damage matrix to structural elements, so an
# unpaired mention is read as structural-but-unlocated, not dismissed.
CRITICIDAD_DEFAULT = 0.6
# A generic mention must not double-count when a specific one fired in the
# same sentence (e.g. "grietas diagonales" detects both t_grieta and
# t_grieta_diagonal).
SUPERSEDE = {
    "grieta_generica": {"grieta_diagonal", "grieta_horizontal_flexion"},
    "fisura_generica": {"grieta_diagonal", "grieta_horizontal_flexion",
                        "grieta_generica"},
    "perdida_recubrimiento": {"acero_expuesto"},
}


@lru_cache(maxsize=1)
def _kg():
    kg = json.loads(KG_PATH.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in kg["nodes"]}
    detecta = []  # (compiled_regex, target_id, target_tipo)
    for e in kg["edges"]:
        if e["relacion"] == "detecta":
            pat = nodes[e["de"]]["patron"]
            detecta.append((re.compile(pat, re.IGNORECASE),
                            e["a"], nodes[e["a"]]["tipo"]))
    peso_pat = {}   # patologia -> max peso of its `indica` edges
    for e in kg["edges"]:
        if e["relacion"] == "indica":
            peso_pat[e["de"]] = max(peso_pat.get(e["de"], 0.0), e["peso"])
    criticidad = {e["de"]: e["peso"] for e in kg["edges"]
                  if e["relacion"] == "pondera"}
    return nodes, detecta, peso_pat, criticidad


def _negated(sentence: str, start: int) -> bool:
    return bool(NEG_RE.search(sentence[:start]))


def analizar(texto: str) -> dict:
    nodes, detecta, peso_pat, criticidad = _kg()
    hallazgos = []   # (patologia, elemento|None)
    acciones = set()
    for sent in SENT_SPLIT.split(str(texto or "")):
        if not sent.strip():
            continue
        pats, elems = [], []
        for rx, target, tipo in detecta:
            m = rx.search(sent)
            if not m or _negated(sent, m.start()):
                continue
            if tipo == "patologia":
                pats.append(target)
            elif tipo == "elemento":
                elems.append(target)
            elif tipo == "accion":
                acciones.add(target)
        for p in pats:
            if SUPERSEDE.get(p, set()) & set(pats):
                continue
            # pair with the most critical co-occurring element, if any
            best = max(elems, key=lambda el: criticidad.get(el, 0.0), default=None)
            hallazgos.append((p, best))

    vistos, gravedad, salida = set(), 0.0, []
    for p, el in sorted(hallazgos,
                        key=lambda h: -criticidad.get(h[1], CRITICIDAD_DEFAULT)):
        if p in vistos:  # count each pathology once, at its strongest pairing
            continue
        vistos.add(p)
        crit = criticidad.get(el, CRITICIDAD_DEFAULT) if el else CRITICIDAD_DEFAULT
        gravedad += peso_pat.get(p, 0.3) * crit
        f = nodes[p].get("fuente", {})
        salida.append((p, el, f"{f.get('doc', '')} {f.get('ref', '')}".strip()))
    return {"hallazgos": salida, "acciones": acciones,
            "gravedad_texto": round(min(1.0, gravedad), 3)}


def _selfcheck():
    r = analizar("Se observan grietas diagonales en muros de carga. "
                 "Columna con aplastamiento y acero expuesto.")
    ids = [h[0] for h in r["hallazgos"]]
    assert "grieta_diagonal" in ids and "aplastamiento" in ids, r
    pares = {h[0]: h[1] for h in r["hallazgos"]}
    assert pares["grieta_diagonal"] == "muro_carga", pares
    assert pares["aplastamiento"] == "columna", pares
    assert all(h[2] for h in r["hallazgos"]), "hallazgo sin cita"
    assert r["gravedad_texto"] > 0.5, r["gravedad_texto"]

    r = analizar("No se observan grietas en los muros. Sin asentamientos.")
    assert not r["hallazgos"], r  # negation per sentence

    r = analizar("Se recomienda demolici�n de la edificaci�n")  # mojibake
    assert "demoler" in r["acciones"], r

    r = analizar("Muros en buen estado")  # element alone -> no finding
    assert not r["hallazgos"] and r["gravedad_texto"] == 0.0, r

    r = analizar("fisuras leves en pañete")  # non-structural, low weight
    assert r["gravedad_texto"] < 0.35, r

    assert analizar("")["gravedad_texto"] == 0.0
    print("selfcheck ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _selfcheck()
    else:
        print(json.dumps(analizar(" ".join(sys.argv[1:])), ensure_ascii=False,
                         indent=2, default=list))
