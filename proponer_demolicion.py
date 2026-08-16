"""Demolition-candidate list from EDAN-F3 `tabla_normalizada`.

Applies the structural criteria of the EDE form (20240926 Formulario de
evaluacion EDE) to every normalized inspection and writes a bounded, ranked
list to the `propuestos_demoler` tab of the EDAN-F3 spreadsheet.

Methodology (structural-engineering reading of the form):

Gate — a building can only be proposed for demolition when the inspection
itself declares it structurally unsafe: total collapse (5.1) or habitability
verdict I2/I3 (7. insegura por dano estructural). Buildings rated H/R1/R2/I1
never enter the list regardless of partial damage answers.

Score (0-100) over the gated set, additive and null-safe:

    colapso_total (5.1)                              -> 100 (automatic, tier 1)
    colapso_parcial (5.2)                            -> 25
    danos_estructura (5.7) severo/moderado           -> 20 / 10
    severidad max(6.2 reportada, calculada)          -> up to 15
    inclinacion_importante (5.4)                     -> 12
    asentamiento_severo (5.3)                        -> 10
    danos_contrapiso_entrepiso_muroscont (5.8)       -> 8 / 4
    estado_edificacion (3.8) malo                    -> 5
    criterio_habitabilidad (7) I3                    -> 5
    mencion explicita de demolicion                  -> +20 / +10 (cap 100)

Explicit demolition mention is the strongest single signal and drives the
tiers: the structured `recomendaciones` field carrying the `demoler` token
(the inspector's own recommendation, +20) or a free-text demolition
recommendation in observaciones/eval_* (negation-guarded, +10).

Tiers (non-collapse): explicit mention AND base score >= 50 ->
`demolicion_prioritaria`; no mention but base score >= 70 ->
`demolicion_probable` (severe damage, needs on-site verification before
committing to demolition). Everything else is dropped so the list stays
bounded and anchored on what the inspectors explicitly wrote.

Only the `propuestos_demoler` tab is written; everything else is read-only.

    python proponer_demolicion.py --check   # offline self-check, no network
    python proponer_demolicion.py --dry     # real data, output/propuestos_demoler.xlsx only
    python proponer_demolicion.py           # real data, write the tab
"""
from __future__ import annotations

import re
import sys
from datetime import datetime

import gspread
import pandas as pd

from integracion.gauth import credentials

F3_SPREADSHEET_ID = "19k--nAEScol_3E7nbSpPev07gW2_UT8ojSsaMGbn6Ds"
SRC_TAB = "tabla_normalizada"
DST_TAB = "propuestos_demoler"
READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
WRITE = ["https://www.googleapis.com/auth/spreadsheets"]

SEVERIDAD = {"alto": 1.0, "medio_alto": 0.6, "medio": 0.3, "bajo": 0.1}
UNSAFE_HABITABILIDAD = {"i2", "i3"}  # insegura por dano estructural
DANO = {"severo": 1.0, "moderado": 0.5}

TEXT_FIELDS = ("observaciones", "eval_estructural", "eval_otra",
               "observaciones_generales")
# �: raw_data headers/text carry mojibake, so "demolici?n" may appear with
# the replacement char where the accent was.
DEMOL_RE = re.compile(r"demol|derrib|desmont", re.IGNORECASE)
NEG_RE = re.compile(r"no\s+(se\s+)?(requiere|recomienda|amerita|necesita)"
                    r"[^.]{0,40}(demol|derrib|desmont)", re.IGNORECASE)

OUT_COLS = ["prioridad", "id_edan", "categoria", "score", "motivos",
            "direccion", "direccion_norm", "barrio_vereda", "comuna", "coords",
            "n_pisos", "n_ocupantes", "criterio_habitabilidad",
            "estado_edificacion", "colapso_total", "colapso_parcial",
            "asentamiento_severo", "inclinacion_importante", "danos_estructura",
            "danos_contrapiso_entrepiso_muroscont", "severidad_danos",
            "severidad_danos_calc", "recomendaciones", "fecha_inspeccion",
            "fecha_corrida"]


def _s(row, col) -> str:
    v = row.get(col, "")
    s = str(v).strip().lower()
    return "" if s in ("nan", "none") else s


def mencion_demolicion(row) -> tuple[bool, bool]:
    """(recomendacion `demoler` marcada, mencion en texto libre sin negacion)."""
    rec = "demoler" in [t.strip() for t in _s(row, "recomendaciones").split(",")]
    txt = " | ".join(str(row.get(f, "") or "") for f in TEXT_FIELDS)
    texto = bool(DEMOL_RE.search(txt)) and not NEG_RE.search(txt)
    return rec, texto


def evaluar(row) -> tuple[int, int, list[str]] | None:
    """(score, base_score, motivos) for a gated row; None when the building is
    not structurally unsafe per the inspection itself."""
    colapso_total = _s(row, "colapso_total") == "si"
    habitabilidad = _s(row, "criterio_habitabilidad")
    if not colapso_total and habitabilidad not in UNSAFE_HABITABILIDAD:
        return None

    if colapso_total:
        return 100, 100, ["5.1 colapso total"]

    score, motivos = 0.0, []
    if _s(row, "colapso_parcial") == "si":
        score += 25
        motivos.append("5.2 colapso parcial")
    d = DANO.get(_s(row, "danos_estructura"), 0.0)
    if d:
        score += 20 * d
        motivos.append(f"5.7 dano estructural {_s(row, 'danos_estructura')}")
    sev = max(SEVERIDAD.get(_s(row, "severidad_danos"), 0.0),
              SEVERIDAD.get(_s(row, "severidad_danos_calc"), 0.0))
    if sev:
        score += 15 * sev
        if sev >= 0.6:
            motivos.append("6.2 severidad alta")
    if _s(row, "inclinacion_importante") == "si":
        score += 12
        motivos.append("5.4 inclinacion importante")
    if _s(row, "asentamiento_severo") == "si":
        score += 10
        motivos.append("5.3 asentamiento severo")
    d = DANO.get(_s(row, "danos_contrapiso_entrepiso_muroscont"), 0.0)
    if d:
        score += 8 * d
        if d == 1.0:
            motivos.append("5.8 dano severo contrapiso/entrepiso/muros contencion")
    if _s(row, "estado_edificacion") == "malo":
        score += 5
        motivos.append("3.8 estado malo")
    if habitabilidad == "i3":
        score += 5
    motivos.insert(0, f"7. habitabilidad {habitabilidad.upper()}")

    base = round(score)
    rec, texto = mencion_demolicion(row)
    if rec:
        score += 20
        motivos.append("recomendacion explicita: demoler")
    if texto:
        score += 10
        motivos.append("texto libre recomienda demolicion")
    return min(100, round(score)), base, motivos


def build_propuestos(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    rows = []
    for _, src in df.iterrows():
        res = evaluar(src)
        if res is None:
            continue
        score, base, motivos = res
        colapso = _s(src, "colapso_total") == "si"
        mencion = any(mencion_demolicion(src))
        if colapso:
            categoria = "demolicion_inmediata"
        elif mencion and base >= 50:
            categoria = "demolicion_prioritaria"
        elif not mencion and base >= 70:
            categoria = "demolicion_probable"
        else:
            continue
        rows.append({
            "id_edan": src.get("id_edan", ""),
            "categoria": categoria,
            "score": score,
            "motivos": "; ".join(motivos),
            **{c: src.get(c, "") for c in OUT_COLS
               if c not in ("prioridad", "id_edan", "categoria", "score",
                            "motivos", "fecha_corrida")},
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=OUT_COLS)
    out = (out.sort_values(["score", "id_edan"], ascending=[False, True])
              .reset_index(drop=True))
    out.insert(0, "prioridad", out.index + 1)
    out["fecha_corrida"] = now.strftime("%Y-%m-%d %H:%M")
    return out[OUT_COLS]


def _selfcheck():
    now = datetime(2026, 8, 16, 12, 0)
    df = pd.DataFrame([
        # total collapse -> tier 1, score 100, always in
        {"id_edan": "AAA01", "colapso_total": "si", "criterio_habitabilidad": "i2"},
        # habitable -> gated out even with severe answers
        {"id_edan": "BBB02", "colapso_total": "no", "criterio_habitabilidad": "h",
         "colapso_parcial": "si", "danos_estructura": "severo",
         "severidad_danos": "alto"},
        # i2 + everything severe but NO explicit mention -> probable
        {"id_edan": "CCC03", "colapso_total": "no", "criterio_habitabilidad": "i2",
         "colapso_parcial": "si", "danos_estructura": "severo",
         "severidad_danos": "alto", "inclinacion_importante": "si",
         "asentamiento_severo": "si",
         "danos_contrapiso_entrepiso_muroscont": "severo",
         "estado_edificacion": "malo"},
        # i2 with moderate damage only -> below 50, dropped (bounded list)
        {"id_edan": "DDD04", "colapso_total": "no", "criterio_habitabilidad": "i2",
         "danos_estructura": "moderado", "severidad_danos": "medio"},
        # i3, mid base score, no mention -> dropped under the tightened rules
        {"id_edan": "EEE05", "colapso_total": "no", "criterio_habitabilidad": "i3",
         "colapso_parcial": "si", "danos_estructura": "severo",
         "severidad_danos_calc": "medio"},
        # i2, inspector ticked `demoler` in recomendaciones -> prioritaria
        {"id_edan": "FFF06", "colapso_total": "no", "criterio_habitabilidad": "i2",
         "colapso_parcial": "si", "danos_estructura": "severo",
         "severidad_danos": "medio",
         "recomendaciones": "evacuar_total,monitoreo,demoler"},
        # i2, free text explicitly NEGATES demolition -> mention must not fire
        {"id_edan": "GGG07", "colapso_total": "no", "criterio_habitabilidad": "i2",
         "colapso_parcial": "si", "danos_estructura": "severo",
         "severidad_danos": "medio",
         "observaciones_generales": "no se requiere por ahora demolicion"},
        # i2, free text recommends demolition -> prioritaria via text signal
        {"id_edan": "HHH08", "colapso_total": "no", "criterio_habitabilidad": "i2",
         "colapso_parcial": "si", "danos_estructura": "severo",
         "severidad_danos": "medio",
         "eval_estructural": "Se recomienda demolicion de la edificacion"},
    ])
    out = build_propuestos(df, now)
    ids = list(out["id_edan"])
    assert "BBB02" not in ids and "DDD04" not in ids and "EEE05" not in ids, ids
    assert "GGG07" not in ids, ids  # negated text never counts as a mention
    assert ids[0] == "AAA01" and out.iloc[0]["categoria"] == "demolicion_inmediata"
    r = out[out["id_edan"] == "CCC03"].iloc[0]
    assert r["score"] == 95 and r["categoria"] == "demolicion_probable", r["score"]
    assert "5.7 dano estructural severo" in r["motivos"]
    r = out[out["id_edan"] == "FFF06"].iloc[0]
    base = round(25 + 20 + 15 * 0.3)  # 50 (banker's rounding of 49.5)
    assert base == 50 and r["score"] == base + 20 == 70, r["score"]
    assert r["categoria"] == "demolicion_prioritaria"
    assert "recomendacion explicita: demoler" in r["motivos"]
    r = out[out["id_edan"] == "HHH08"].iloc[0]
    assert r["score"] == 60 and r["categoria"] == "demolicion_prioritaria", r["score"]
    assert "texto libre recomienda demolicion" in r["motivos"]
    assert list(out.columns) == OUT_COLS
    assert (out["score"].diff().dropna() <= 0).all()
    assert build_propuestos(pd.DataFrame(), now).empty
    print("selfcheck ok")


def _read_tab(gc, spreadsheet_id, tab) -> pd.DataFrame:
    vals = gc.open_by_key(spreadsheet_id).worksheet(tab).get_all_values()
    if not vals:
        return pd.DataFrame()
    header, data = vals[0], vals[1:]
    width = len(header)
    data = [(r + [""] * width)[:width] for r in data]
    return pd.DataFrame(data, columns=header)


def main() -> None:
    if "--check" in sys.argv:
        _selfcheck()
        return

    gc = gspread.authorize(credentials(READONLY))
    df = _read_tab(gc, F3_SPREADSHEET_ID, SRC_TAB)
    print(f"{SRC_TAB}: {len(df)} registros")
    out = build_propuestos(df, datetime.now())
    counts = out["categoria"].value_counts().to_dict() if not out.empty else {}
    print(f"propuestos: {len(out)} | {counts}")

    if "--dry" in sys.argv:
        path = "output/propuestos_demoler.xlsx"
        out.to_excel(path, index=False)
        print(f"dry run: wrote {path} (no sheet write)")
        return

    ss = gspread.authorize(credentials(WRITE)).open_by_key(F3_SPREADSHEET_ID)
    try:
        dst = ss.worksheet(DST_TAB)
    except gspread.WorksheetNotFound:
        dst = ss.add_worksheet(DST_TAB, rows=len(out) + 10, cols=len(OUT_COLS))
    values = [OUT_COLS] + out.astype(str).where(out.notna(), "").values.tolist()
    dst.clear()
    dst.update(values=values, range_name="A1", value_input_option="RAW")
    print(f"wrote {len(out)} rows to {DST_TAB}")


if __name__ == "__main__":
    main()
