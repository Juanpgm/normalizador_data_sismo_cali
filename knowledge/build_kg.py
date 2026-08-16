"""Knowledge-graph tooling for the demolition-criteria pipeline.

Two jobs:

    python knowledge/build_kg.py --extract   # dump fuentes/*.pdf -> fuentes/*.txt
    python knowledge/build_kg.py --check     # validate kg.json integrity

`kg.json` is a curated graph (hand-authored from the extracted sources, every
node and edge carries a citation) that `analisis_texto.py` consumes. The
extractor exists so the curation is reproducible and auditable: each page is
marked `[[pag N]]` in the .txt, and citations in the graph reference those
pages.

Schema:
    nodes: [{id, tipo, nombre, descripcion?, fuente: {doc, ref}}]
        tipo in {patologia, elemento, criterio_norma, accion, termino}
        termino nodes also carry `patron` (regex fragment, case-insensitive).
    edges: [{de, a, relacion, peso?, fuente?}]
        relacion in {indica, afecta, sugiere, detecta, pondera}
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FUENTES = HERE / "fuentes"
KG_PATH = HERE / "kg.json"

TIPOS = {"patologia", "elemento", "criterio_norma", "accion", "termino"}
RELACIONES = {"indica", "afecta", "sugiere", "detecta", "pondera"}
DOCS = {"NSR-10", "AIS-manual", "ATC-20"}


def extract() -> None:
    from pypdf import PdfReader

    for pdf in sorted(FUENTES.glob("*.pdf")):
        out = pdf.with_suffix(".txt")
        pages = []
        for i, page in enumerate(PdfReader(pdf).pages, 1):
            pages.append(f"[[pag {i}]]\n{page.extract_text() or ''}")
        out.write_text("\n".join(pages), encoding="utf-8")
        print(f"{pdf.name}: {i} pages -> {out.name}")


def check() -> None:
    kg = json.loads(KG_PATH.read_text(encoding="utf-8"))
    nodes, edges = kg["nodes"], kg["edges"]
    ids = [n["id"] for n in nodes]
    assert len(ids) == len(set(ids)), "duplicate node ids"
    idset = set(ids)
    for n in nodes:
        assert n["tipo"] in TIPOS, f"{n['id']}: bad tipo {n['tipo']}"
        f = n.get("fuente")
        assert f and f.get("doc") in DOCS and f.get("ref"), \
            f"{n['id']}: missing/invalid fuente"
        if n["tipo"] == "termino":
            assert n.get("patron"), f"{n['id']}: termino sin patron"
            re.compile(n["patron"], re.IGNORECASE)  # must be valid regex
    for e in edges:
        assert e["de"] in idset and e["a"] in idset, f"dangling edge {e}"
        assert e["relacion"] in RELACIONES, f"bad relacion {e}"
        if e["relacion"] in ("indica", "pondera"):
            assert isinstance(e.get("peso"), (int, float)) and 0 <= e["peso"] <= 1, \
                f"{e['de']}->{e['a']}: peso 0-1 requerido"
    # every termino must detect something; every patologia should be detectable
    det_from = {e["de"] for e in edges if e["relacion"] == "detecta"}
    det_to = {e["a"] for e in edges if e["relacion"] == "detecta"}
    for n in nodes:
        if n["tipo"] == "termino":
            assert n["id"] in det_from, f"{n['id']}: termino sin arista detecta"
        if n["tipo"] == "patologia":
            assert n["id"] in det_to, f"{n['id']}: patologia sin termino que la detecte"
    n_by_tipo = {}
    for n in nodes:
        n_by_tipo[n["tipo"]] = n_by_tipo.get(n["tipo"], 0) + 1
    print(f"kg.json ok: {len(nodes)} nodos {n_by_tipo} | {len(edges)} aristas")


if __name__ == "__main__":
    if "--extract" in sys.argv:
        extract()
    else:
        check()
