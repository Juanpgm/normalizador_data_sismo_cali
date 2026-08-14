"""
Load and clean EDAN and Visitas from Google Sheets. Faithful port of EDA.ipynb
sections 4 (EDAN) and 5 (Visitas), including the deterministic-ID contract:
the persisted id column on each sheet is the source of truth and is reused
verbatim; only rows without an id receive a new deterministic one.

Read-only: this module NEVER writes back to the sheets.
"""
from __future__ import annotations

import random
import re
import string

import gspread
import pandas as pd

from .config import (EDAN_ID_SEED, EDAN_SHEET_NAME, EDAN_SPREADSHEET_ID,
                     VISITAS_ID_SEED, VISITAS_SHEET_NAME,
                     VISITAS_SPREADSHEET_ID)
from .coords import coords_to_wgs84
from .gauth import credentials
from .normalization import add_handshake, normalize_address

_READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Deterministic-ID machinery (identical contract to the notebook) ──────────────
_CHARS = string.ascii_uppercase + string.digits
_ID_AMBIGUO = re.compile(r"\d{4}|\d+E\d+")


def generar_ids(seed, n, taken=()):
    """Deterministic 4-char ID sequence, skipping taken and numeric-lookalikes."""
    rng = random.Random(seed)
    seen, out = set(taken), []
    while len(out) < n:
        cand = "".join(rng.choices(_CHARS, k=4))
        if cand not in seen and not _ID_AMBIGUO.fullmatch(cand):
            seen.add(cand)
            out.append(cand)
    return out


def _read_values(spreadsheet_id, sheet_name) -> pd.DataFrame:
    """Read a sheet as raw text (get_all_values). Unlike get_all_records this
    does NOT numericise cell values, so id columns like "0224" survive verbatim
    and stay row-aligned with their data — the two properties the persisted-id
    contract depends on."""
    gc = gspread.authorize(credentials(_READONLY))
    ws = gc.open_by_key(spreadsheet_id).worksheet(sheet_name)
    vals = ws.get_all_values()
    if not vals:
        return pd.DataFrame()
    header, data = vals[0], vals[1:]
    width = len(header)
    data = [(r + [""] * width)[:width] for r in data]
    return pd.DataFrame(data, columns=header)


def _reuse_or_generate_ids(df, id_col, seed):
    """Reuse the persisted id column (already correct raw text, row-aligned) or
    generate new ids by position. Only blank cells receive a fresh id."""
    if id_col in df.columns:
        cur = df[id_col].astype(str).str.strip()
        nuevos = iter(generar_ids(seed, int(cur.eq("").sum()), taken=set(cur[cur.ne("")])))
        df[id_col] = [c if c else next(nuevos) for c in cur]
    else:
        df[id_col] = generar_ids(seed, len(df))
    df.insert(0, id_col, df.pop(id_col))
    return df


def recover_barrio_header(df: pd.DataFrame) -> pd.DataFrame:
    """Restore the EDAN "Barrio" header when the live sheet loses it.

    Seen in production on 2026-08-14: someone editing the document replaced the
    header with "-", the rename map stopped firing, and every run died on a
    missing `barrio_vereda`. That column is the precision guard of the whole
    cascade, so the pipeline cannot simply continue without it.

    Recovery is deliberately narrow — the column must be unnamed AND sit in its
    exact place between "Zona" and "Tipo" — and it announces itself, because a
    sheet quietly losing headers is something a human has to go fix.
    """
    if "Barrio" in df.columns:
        return df
    cols = list(df.columns)
    try:
        left, right = cols.index("Zona"), cols.index("Tipo")
    except ValueError:
        return df
    if right - left != 2 or str(cols[left + 1]).strip() not in ("", "-"):
        return df
    print(f"[sheets] AVISO: la columna de barrio llegó sin encabezado "
          f"({cols[left + 1]!r}); se recupera por posición entre 'Zona' y 'Tipo'. "
          f"Conviene restaurar el encabezado en la hoja.")
    cols[left + 1] = "Barrio"
    df.columns = cols
    return df


# ── EDAN ──────────────────────────────────────────────────────────────────────
def load_edan() -> pd.DataFrame:
    df = recover_barrio_header(_read_values(EDAN_SPREADSHEET_ID, EDAN_SHEET_NAME))

    df = df.drop(columns=["DESCRIPCION CENTRALIZADA", "REFERENCIADO EN EL MAPA",
                          "Direccion", "Color semaforo"], errors="ignore")
    df = df.rename(columns={
        # The sheet's running number is the consecutive GRED hands to responders.
        # Volunteers copy it into the visit form, which makes it a deterministic
        # join key — see the `preregistro` tier in matching.py.
        "ID": "consecutivo_edan",
        "Prioridad": "prioridad", "Estado": "estado",
        "Comuna - Corregimiento": "comuna_corregimiento", "Zona": "zona",
        "Barrio": "barrio_vereda", "Tipo": "tipo_estructura",
        "Referencia": "punto_referencia", "DIRECCION COMPLETA": "direccion",
        "Descripción": "descripcion", "Normalizacion": "normalizacion",
        "Personas Evacuadas": "n_personas_evacuadas",
        "Seres sintientes": "n_seres_sintientes",
        "Atencion de la respuesta": "personal_atencion",
        "Colapso Total": "n_colapsados_total", "Colapso Parcial": "n_colapsados_parcial",
        "Daños": "n_danos", "Atrapamientos": "n_atrapamientos",
        "Rescatados": "n_rescatados", "Fallecidos": "n_fallecidos",
        "NOMBRE DELEGADO": "nombre_delegado",
        "OBSERVACIONES (Gestiòn del riesgo)": "observaciones_gred",
        "DESAPARECIDOS": "n_desaparecidos", "COORDENADAS": "coords",
        "DESCRIPCION DE LA SITUACION": "descripcion_2",
        "Observaciones generales (FORMULARIO)": "observaciones_generales",
        "NOMBRE LIDER": "nombre_lider", "TELEFONO": "num_telefono", "GRUPO ": "grupo",
    })

    for c in ("prioridad", "direccion", "nombre_lider", "nombre_delegado"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.title()

    # Drop ANULADO and fully-empty rows
    empty_values = {"", "-", " "}
    # A row carrying nothing but its running number is still an empty row, so
    # consecutivo_edan is excluded from the emptiness check alongside sitio_id.
    cols_check = [c for c in df.columns if c not in ("sitio_id", "consecutivo_edan")]
    anulados = df["estado"].astype(str).str.upper() == "ANULADO"
    sin_datos = df[cols_check].apply(
        lambda row: all(str(v).strip() in empty_values for v in row), axis=1)
    df = df[~(anulados | sin_datos)].reset_index(drop=True)

    df = _reuse_or_generate_ids(df, "sitio_id", EDAN_ID_SEED)

    # Group n_ columns after n_personas_evacuadas (as in the notebook)
    if "n_personas_evacuadas" in df.columns:
        all_cols = df.columns.tolist()
        n_cols = [c for c in all_cols if c.startswith("n_")]
        anchor = all_cols.index("n_personas_evacuadas")
        before = [c for c in all_cols[:anchor] if not c.startswith("n_")]
        after = [c for c in all_cols[anchor:] if not c.startswith("n_")]
        order = before + n_cols + after
        if "descripcion" in order and "descripcion_2" in order:
            order = [c for c in order if c != "descripcion_2"]
            order.insert(order.index("descripcion") + 1, "descripcion_2")
        df = df[order]

    df.insert(df.columns.get_loc("direccion") + 1, "direccion_norm",
              df["direccion"].apply(normalize_address))
    df = add_handshake(df, source_col="direccion_norm")
    df["coords"] = df["coords"].apply(lambda v: coords_to_wgs84(v) or v)
    return df


# ── Visitas ───────────────────────────────────────────────────────────────────
def load_visitas() -> pd.DataFrame:
    df = _read_values(VISITAS_SPREADSHEET_ID, VISITAS_SHEET_NAME)

    drop_cols = [c for c in df.columns if c.startswith("Marque con una X")
                 or c in ("Columna 22", "Columna 1", "Colapso")]
    df = df.drop(columns=drop_cols, errors="ignore")
    df = df.rename(columns={
        "Marca temporal": "timestamp", "Dirección de correo electrónico": "email",
        "Dirección completa": "direccion", "Coordenadas Geográficas": "coords",
        "Descripción completa de la situación encontrada": "descripcion",
        "Cantidad de personas fallecidos (EN NÚMEROS POR FAVOR)": "n_fallecidos",
        "Cantidad de personas atrapadas  (EN NÚMEROS POR FAVOR)": "n_atrapamientos",
        "Cantidad de personas rescatadas  (EN NÚMEROS POR FAVOR)": "n_rescatados",
        "Suba los soportes que considere necesarios (Vídeos, fotos, audios, documentos)": "evidencia_soporte",
        "Nombre de la persona que diligencia el reporte": "nombre_diligenciador",
        "¿Se necesita evacuación preventiva de las personas?": "evacuacion_preventiva",
        "Nombre del organismo que pertenece. \n\nEn caso de ser parte del grupo de ingenieros y arquitectos voluntarios ir a la siguiente pregunta": "nombre_organismo",
        "Nivel de riesgo ": "nivel_riesgo",
        "Consecutivo - id (solo si fue asignado)": "consecutivo_gred",
        "¿La edificación requiere demolición?": "requiere_demolicion",
        "Describa las observaciones que considere pertinentes a la pregunta anterior": "observaciones",
        "Requieren evacuación": "requieren_evacuacion",
        "Observaciones generales": "observaciones_generales", "Comuna": "comuna_corregimiento",
        "Barrio": "barrio_vereda", "Tipo de edificación atendida": "tipo_estructura",
        "Situación de la vivienda": "estado_estructura",
    })
    # Long-titled numeric/name columns → short names (match by prefix)
    for col in list(df.columns):
        low = col.lower()
        if low.startswith("cantidad de personas aproximadas"):
            df = df.rename(columns={col: "personas_necesitan_evacuar"})
        elif low.startswith("cantidad de casas"):
            df = df.rename(columns={col: "n_estructuras_caracterizadas"})
        elif low.startswith("nombre del edificio"):
            df = df.rename(columns={col: "nombre_estructura"})

    for c in ("barrio_vereda", "comuna_corregimiento", "tipo_estructura",
              "nombre_diligenciador", "nombre_estructura", "estado_estructura",
              "requiere_demolicion", "direccion"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.title()

    df = _reuse_or_generate_ids(df, "visita_id", VISITAS_ID_SEED)

    # evidencia_soporte is a comma-separated list of Drive links, not a single
    # value. The count makes it filterable in the published sheet without
    # anyone having to parse the URLs.
    if "evidencia_soporte" in df.columns:
        from .exif_coords import extract_file_ids
        df["n_evidencias"] = df["evidencia_soporte"].map(lambda c: len(extract_file_ids(c)))

    df.insert(df.columns.get_loc("direccion") + 1, "direccion_norm",
              df["direccion"].apply(normalize_address))

    _empty = {"", "-", " "}

    def _with_location(row):
        norm = str(row["direccion_norm"]).strip()
        if norm in _empty:
            return norm
        barrio = str(row["barrio_vereda"]).strip()
        if barrio and barrio not in _empty:
            return f"{norm}, {barrio}, Cali"
        return f"{norm}, Cali"

    df["direccion_norm"] = df.apply(_with_location, axis=1)
    df = add_handshake(df, source_col="direccion_norm")
    df["coords"] = df["coords"].apply(lambda v: coords_to_wgs84(v) or v)
    return df
