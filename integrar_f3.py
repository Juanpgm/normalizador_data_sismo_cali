"""Match EDAN-F3 records against the EDAN SISMO integrated table.

Reads `tabla_normalizada` from the EDAN-F3 sheet and `tabla_integrada` from the
EDAN SISMO sheet, runs the existing 7-tier matching cascade (address + coords)
framing tabla_integrada as the "edan" side and F3 as the "visitas" side, and
writes the COMPLETE tabla_integrada listing to the `integracion_f3` tab: one row
per registro (one per match pair when several F3 points hit the same registro).
Rows with an F3 match carry edan_id and the registro_id concatenated with it;
rows with empty edan_id are the pending list — sites with no F3 inspection yet,
ready for assignment. Finally, F3 points with NO registro in tabla_integrada
(brand-new sites) are appended at the end, tagged `match_method = solo_f3`.

Only the `integracion_f3` tab is ever written. `tabla_integrada` and
`tabla_normalizada` are read-only here.

    python integrar_f3.py --check   # offline self-check, no network
    python integrar_f3.py --dry     # real match, write output/integracion_f3.xlsx only
    python integrar_f3.py           # real match, write the integracion_f3 tab
"""
from __future__ import annotations

import sys
from collections import Counter

import gspread
import pandas as pd

from integracion.config import EDAN_SPREADSHEET_ID
from integracion.gauth import credentials
from integracion.matching import build_match_table

F3_SPREADSHEET_ID = "19k--nAEScol_3E7nbSpPev07gW2_UT8ojSsaMGbn6Ds"
F3_SRC_TAB = "tabla_normalizada"
DST_TAB = "integracion_f3"
INTEGRADA_TAB = "tabla_integrada"
READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
WRITE = ["https://www.googleapis.com/auth/spreadsheets"]

OUT_COLS = ["registro_id", "sitio_id", "visita_id", "edan_id", "direccion",
            "barrio_vereda", "comuna_corregimiento", "coords", "tipo_estructura",
            "match_method", "match_score"]


def _read_tab(gc, spreadsheet_id, tab) -> pd.DataFrame:
    """Raw-text, row-aligned read (same contract as io_sheets._read_values)."""
    vals = gc.open_by_key(spreadsheet_id).worksheet(tab).get_all_values()
    if not vals:
        return pd.DataFrame()
    header, data = vals[0], vals[1:]
    width = len(header)
    data = [(r + [""] * width)[:width] for r in data]
    return pd.DataFrame(data, columns=header)


def build_integracion_f3(df_integrada: pd.DataFrame, df_f3: pd.DataFrame) -> pd.DataFrame:
    """Complete tabla_integrada listing: one row per registro, one per match
    pair when several F3 points hit the same registro. Empty edan_id = pending."""
    # Frame both sides into the shape build_match_table expects.
    E = pd.DataFrame({
        "sitio_id": df_integrada["registro_id"],
        "direccion_norm": df_integrada["direccion_unificada"],
        "barrio_vereda": df_integrada["barrio_unificado"],
        "coords": df_integrada["coords_unificadas"],
    })
    V = pd.DataFrame({
        "visita_id": df_f3["id_edan"],
        "direccion_norm": df_f3["direccion_norm"],
        "barrio_vereda": df_f3["barrio_vereda"],
        "coords": df_f3["coords"],
    })
    matches = build_match_table(E, V)

    # Invert: registro_id -> list of (edan_id, method, score)
    by_registro: dict[str, list] = {}
    for _, m in matches.iterrows():
        rid = m["sitio_id"]
        if pd.notna(rid) and str(rid).strip():
            by_registro.setdefault(str(rid), []).append(
                (m["visita_id"], m["match_method"], m["match_score"]))

    rows = []
    for _, src in df_integrada.iterrows():
        rid = str(src["registro_id"])
        te = str(src.get("tipo_estructura_edan", "")).strip()
        tv = str(src.get("tipo_estructura_visita", "")).strip()
        base = {
            "sitio_id": src["sitio_id"],
            "visita_id": src["visita_id"],
            "direccion": src["direccion_unificada"],
            "barrio_vereda": src["barrio_unificado"],
            "comuna_corregimiento": src["comuna_unificada"],
            "coords": src["coords_unificadas"],
            "tipo_estructura": te or tv,
        }
        hits = by_registro.get(rid, [])
        if hits:
            for edan_id, method, score in hits:
                rows.append({**base, "registro_id": f"{rid}-{edan_id}",
                             "edan_id": edan_id, "match_method": method,
                             "match_score": score})
        else:
            rows.append({**base, "registro_id": rid, "edan_id": "",
                         "match_method": "", "match_score": ""})

    # Append F3 points with no registro in tabla_integrada (brand-new sites).
    matched_edan = {edan for hits in by_registro.values() for edan, _, _ in hits}
    for _, f3 in df_f3.iterrows():
        if f3["id_edan"] in matched_edan:
            continue
        rows.append({
            "registro_id": "", "sitio_id": "", "visita_id": "",
            "edan_id": f3["id_edan"],
            "direccion": f3["direccion_norm"],
            "barrio_vereda": f3["barrio_vereda"],
            "comuna_corregimiento": f3.get("comuna", ""),
            "coords": f3["coords"],
            "tipo_estructura": str(f3.get("uso_edificacion", "")).strip(),
            "match_method": "solo_f3", "match_score": "",
        })
    return pd.DataFrame(rows, columns=OUT_COLS)


def _selfcheck():
    df_integrada = pd.DataFrame({
        "registro_id": ["FSQG-0E2M", "ABCD-1234", "ZZZZ-9999"],
        "sitio_id": ["FSQG", "ABCD", "ZZZZ"],
        "visita_id": ["0E2M", "1234", "9999"],
        "direccion_unificada": ["CL 46 # 45-10, Centro, Cali",
                                "KR 99 # 11-22, Lejos, Cali", "Sin direccion util"],
        "barrio_unificado": ["Centro", "Lejos", ""],
        "comuna_unificada": ["Comuna 3", "Comuna 99", ""],
        "coords_unificadas": ["", "", "3.415600, -76.541900"],
        "tipo_estructura_edan": ["Edificio", "Casa", ""],
        "tipo_estructura_visita": ["", "", "Bodega"],
    })
    df_f3 = pd.DataFrame({
        "id_edan": ["AAA11", "BBB22", "CCC33"],
        "direccion_norm": ["CL 46 # 45-10", "Punto sin nomenclatura", "CL 1 # 2-3"],
        "barrio_vereda": ["Centro", "", "Otro"],
        "coords": ["", "3.415592, -76.541889", ""],
        "comuna": ["Comuna 3", "Comuna 3", "Comuna 9"],
        "uso_edificacion": ["residencial", "educativo", "comercial"],
    })
    out = build_integracion_f3(df_integrada, df_f3)
    assert list(out.columns) == OUT_COLS
    assert len(out) == 4  # 3 registros + 1 F3-only appended
    r0 = out.iloc[0]  # handshake match on address -> carries edan_id AAA11
    assert r0["registro_id"] == "FSQG-0E2M-AAA11" and r0["match_method"] == "handshake"
    assert r0["sitio_id"] == "FSQG" and r0["visita_id"] == "0E2M"
    assert r0["edan_id"] == "AAA11" and r0["tipo_estructura"] == "Edificio"
    assert r0["direccion"] == "CL 46 # 45-10, Centro, Cali"
    r1 = out.iloc[1]  # no F3 match -> pending for assignment
    assert r1["registro_id"] == "ABCD-1234" and r1["edan_id"] == ""
    assert r1["match_method"] == "" and r1["tipo_estructura"] == "Casa"
    r2 = out.iloc[2]  # geo match on coords (~1m) -> carries edan_id BBB22
    assert r2["registro_id"] == "ZZZZ-9999-BBB22" and r2["match_method"] == "geo"
    assert r2["edan_id"] == "BBB22" and r2["tipo_estructura"] == "Bodega"
    r3 = out.iloc[3]  # F3 point with no registro -> appended, solo_f3
    assert r3["registro_id"] == "" and r3["edan_id"] == "CCC33"
    assert r3["match_method"] == "solo_f3" and r3["tipo_estructura"] == "comercial"
    print("selfcheck ok")


def main() -> dict:
    if "--check" in sys.argv:
        _selfcheck()
        return {}

    gc = gspread.authorize(credentials(READONLY))
    df_f3 = _read_tab(gc, F3_SPREADSHEET_ID, F3_SRC_TAB)
    df_integrada = _read_tab(gc, EDAN_SPREADSHEET_ID, INTEGRADA_TAB)
    print(f"read {len(df_f3)} F3 rows, {len(df_integrada)} tabla_integrada rows")

    out = build_integracion_f3(df_integrada, df_f3)
    pairs = ~out["match_method"].isin(["", "solo_f3"])            # registro <-> F3 match
    pending = out["edan_id"].astype(str).str.strip().eq("")       # registro sin F3
    solo_f3 = out["match_method"].eq("solo_f3")                   # F3 sin registro
    n_reg_con_f3 = out.loc[pairs, "registro_id"].str.rsplit("-", n=1).str[0].nunique()
    print(f"{len(out)} filas | registros con F3: {n_reg_con_f3} ({int(pairs.sum())} pares) "
          f"| pendientes de asignar: {int(pending.sum())} "
          f"| F3 nuevos anexados: {int(solo_f3.sum())}")
    print("por metodo:", dict(Counter(out.loc[pairs, "match_method"])))
    summary = {"filas": len(out), "registros_con_f3": int(n_reg_con_f3),
               "pares": int(pairs.sum()), "pendientes": int(pending.sum()),
               "f3_nuevos": int(solo_f3.sum())}

    if "--dry" in sys.argv:
        path = "output/integracion_f3.xlsx"
        out.to_excel(path, index=False)
        print(f"dry run: wrote {path} (no sheet write)")
        return summary

    dst = gspread.authorize(credentials(WRITE)).open_by_key(F3_SPREADSHEET_ID).worksheet(DST_TAB)
    values = [OUT_COLS] + out.astype(str).where(out.notna(), "").values.tolist()
    dst.clear()
    dst.update(values=values, range_name="A1", value_input_option="RAW")
    print(f"wrote {len(out)} rows to {DST_TAB}")
    return summary


if __name__ == "__main__":
    main()
