"""Guards for the Google Sheets publisher.

This is the only code that writes into the live EDAN document, so the tests
focus on what must never happen: touching a tab we did not mean to touch,
creating or deleting worksheets, silently dropping records, or shipping a
payload the API cannot serialize.

No network: a fake gspread client records every call.
"""
import math

import numpy as np
import pandas as pd
import pytest

from integracion import config, export_sheets
from integracion.export_sheets import SheetGuardError, push_to_sheets


# ── Fake gspread surface ──────────────────────────────────────────────────────
class FakeWorksheet:
    def __init__(self, title, sheet_id, rows=1000, cols=26):
        self.title, self.id = title, sheet_id
        self.row_count, self.col_count = rows, cols
        self.updates = []      # [(range_name, values)]
        self.resizes = []
        self.cleared = False

    def update(self, values=None, range_name=None, value_input_option=None):
        assert value_input_option == "RAW", "addresses must not be re-interpreted"
        self.updates.append((range_name, values))

    def resize(self, rows=None, cols=None):
        self.resizes.append((rows, cols))
        if rows:
            self.row_count = rows
        if cols:
            self.col_count = cols

    def clear(self):
        self.cleared = True

    @property
    def written(self):
        """All written rows, top-down, as one matrix."""
        out = []
        for _, values in self.updates:
            out.extend(values)
        return out


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self.id = "fake-spreadsheet"
        self._ws = {w.title: w for w in worksheets}
        self.created, self.deleted = [], []

    def worksheet(self, title):
        import gspread
        if title not in self._ws:
            raise gspread.WorksheetNotFound(title)
        return self._ws[title]

    def add_worksheet(self, *a, **k):        # pragma: no cover - must never run
        self.created.append((a, k))
        raise AssertionError("the publisher must never create a worksheet")

    def del_worksheet(self, *a, **k):        # pragma: no cover - must never run
        self.deleted.append((a, k))
        raise AssertionError("the publisher must never delete a worksheet")


class FakeClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet
        self.opened = []

    def open_by_key(self, key):
        self.opened.append(key)
        return self.spreadsheet


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture
def sheets():
    tabla = FakeWorksheet(config.TABLA_SHEET_NAME, config.TABLA_SHEET_ID)
    stats = FakeWorksheet(config.STATS_SHEET_NAME, config.STATS_SHEET_ID)
    other = FakeWorksheet("EDAN 100826 - Datos Madre", 571799645, rows=2128, cols=54)
    return tabla, stats, other


@pytest.fixture
def client(sheets):
    return FakeClient(FakeSpreadsheet(list(sheets)))


@pytest.fixture
def df_consolidada():
    """Mixed sources on purpose: a matched pair, an EDAN-only site and a
    visita-only report. All three must reach the sheet."""
    df = pd.DataFrame({
        "fuente": ["edan+visita", "solo_edan", "solo_visita"],
        "trust_score": [0.95, np.nan, 0.30],
        "match_method": ["handshake", None, None],
        "sitio_id": ["AB12", "CD34", None],
        "visita_id": ["EF56", None, "GH78"],
        "direccion_unificada": ["CL 8 #38-120", "KR 15 #4-10", "AV 6 #22-3"],
        "lat": [3.42, np.nan, 3.41],
        "n_fallecidos_total": [np.int64(0), np.int64(2), np.nan],
    }, index=pd.Index(["R1", "R2", "R3"], name="registro_id"))
    return df


@pytest.fixture
def match_table():
    return pd.DataFrame({
        "visita_id": ["EF56", "GH78"],
        "sitio_id": ["AB12", None],
        "match_method": ["handshake", None],
        "match_score": [1.0, np.nan],
        "trust": [0.95, np.nan],
    })


@pytest.fixture
def report():
    return {
        "matching": {"visitas_total": 2, "visitas_emparejadas": 1,
                     "tasa_match_pct": 50.0, "por_metodo": {"handshake": 1},
                     "trust": {"mean": 0.95, "min": 0.95, "median": 0.95}},
        "integracion": {"registros": 3, "columnas": 8,
                        "por_fuente": {"edan+visita": 1, "solo_edan": 1,
                                       "solo_visita": 1}},
    }


# ── Destination safety ────────────────────────────────────────────────────────
def test_writes_only_the_two_target_worksheets(client, sheets, df_consolidada,
                                               match_table, report):
    tabla, stats, other = sheets
    push_to_sheets(df_consolidada, match_table, report, client=client)

    assert tabla.updates and stats.updates
    assert other.updates == [], "no other tab may be written"
    assert other.resizes == [], "no other tab may be resized"
    assert not any(ws.cleared for ws in sheets), "clear() would empty the tab on failure"
    assert client.spreadsheet.created == [] and client.spreadsheet.deleted == []


def test_renamed_or_recreated_tab_aborts_before_writing(client, sheets,
                                                        df_consolidada,
                                                        match_table, report):
    tabla, stats, _ = sheets
    tabla.id = config.TABLA_SHEET_ID + 1          # tab was recreated

    with pytest.raises(SheetGuardError, match="sheetId"):
        push_to_sheets(df_consolidada, match_table, report, client=client)

    assert tabla.updates == [] and stats.updates == []


def test_missing_tab_raises_instead_of_creating_it(client, sheets, df_consolidada,
                                                   match_table, report):
    del client.spreadsheet._ws[config.STATS_SHEET_NAME]

    with pytest.raises(SheetGuardError, match="does not exist"):
        push_to_sheets(df_consolidada, match_table, report, client=client)


# ── Payload: every record, no filtering ───────────────────────────────────────
def test_publishes_every_record_including_unmatched(client, sheets, df_consolidada,
                                                    match_table, report):
    tabla, _, _ = sheets
    push_to_sheets(df_consolidada, match_table, report, client=client)

    rows = tabla.written
    header, data = rows[0], rows[1:len(df_consolidada) + 1]
    assert header[0] == "registro_id"
    assert [r[0] for r in data] == ["R1", "R2", "R3"]

    fuente = header.index("fuente")
    assert {r[fuente] for r in data} == {"edan+visita", "solo_edan", "solo_visita"}


def test_unmatched_rows_keep_blank_ids_not_dropped(client, sheets, df_consolidada,
                                                   match_table, report):
    tabla, _, _ = sheets
    push_to_sheets(df_consolidada, match_table, report, client=client)

    rows = tabla.written
    header = rows[0]
    sitio, visita = header.index("sitio_id"), header.index("visita_id")
    solo_edan = rows[2]
    solo_visita = rows[3]
    assert solo_edan[visita] == "" and solo_edan[sitio] == "CD34"
    assert solo_visita[sitio] == "" and solo_visita[visita] == "GH78"


def test_stale_rows_are_blanked_by_padding(client, sheets, df_consolidada,
                                           match_table, report):
    """The tab held 1000 rows; 4 are real. The other 996 must be overwritten
    with blanks in the same pass, never left behind."""
    tabla, _, _ = sheets
    push_to_sheets(df_consolidada, match_table, report, client=client)

    rows = tabla.written
    assert len(rows) == 1000
    assert all(cell == "" for row in rows[4:] for cell in row)


def test_grid_is_trimmed_to_the_real_extent(client, sheets, df_consolidada,
                                            match_table, report):
    """After blanking the stale rows the grid shrinks, so the next run does not
    re-write hundreds of empty rows every hour."""
    tabla, stats, _ = sheets
    push_to_sheets(df_consolidada, match_table, report, client=client)

    assert tabla.row_count == len(df_consolidada) + 1
    assert stats.row_count < 1000


def test_grid_grows_when_the_payload_is_taller_than_the_tab(client, sheets,
                                                            match_table, report):
    tabla, _, _ = sheets
    tabla.row_count = 10
    big = pd.DataFrame({"fuente": ["solo_edan"] * 50, "sitio_id": [f"S{i}" for i in range(50)]},
                       index=pd.Index([f"R{i}" for i in range(50)], name="registro_id"))

    push_to_sheets(big, match_table, report, client=client)

    assert tabla.row_count >= 51
    assert len(tabla.written) == 51


# ── Payload: serialization ────────────────────────────────────────────────────
def test_payload_is_json_safe(client, sheets, df_consolidada, match_table, report):
    tabla, stats, _ = sheets
    push_to_sheets(df_consolidada, match_table, report, client=client)

    for ws in (tabla, stats):
        for row in ws.written:
            for cell in row:
                assert isinstance(cell, (str, int, float)), type(cell)
                assert not (isinstance(cell, float) and math.isnan(cell))
                assert not isinstance(cell, np.generic)


def test_missing_values_become_empty_strings(client, sheets, df_consolidada,
                                             match_table, report):
    tabla, _, _ = sheets
    push_to_sheets(df_consolidada, match_table, report, client=client)

    header = tabla.written[0]
    lat = header.index("lat")
    assert tabla.written[2][lat] == ""      # solo_edan has no coordinates


def test_long_text_is_truncated_to_the_cell_limit():
    cell = export_sheets._cell("x" * (export_sheets.MAX_CELL_CHARS + 500))
    assert len(cell) == export_sheets.MAX_CELL_CHARS


# ── Stats sheet ───────────────────────────────────────────────────────────────
def test_stats_carry_every_report_metric(client, sheets, df_consolidada,
                                         match_table, report):
    _, stats, _ = sheets
    push_to_sheets(df_consolidada, match_table, report, client=client)

    rows = stats.written
    assert rows[0][:3] == ["seccion", "metrica", "valor"]
    published = {(r[0], r[1]) for r in rows[1:] if r[0]}

    from integracion.metrics import flatten
    for section in ("matching", "integracion"):
        for key in flatten(report[section]):
            assert (section, key) in published, f"missing {section}.{key}"


def test_stats_carry_every_threshold(client, sheets, df_consolidada, match_table,
                                     report):
    _, stats, _ = sheets
    push_to_sheets(df_consolidada, match_table, report, client=client)

    umbrales = {r[1] for r in stats.written if r and r[0] == "umbrales"}
    assert {"vector_tol", "tfidf_min", "fuzzy_threshold", "embedding_min_sim",
            "trust_min", "geo_near_m", "geo_far_m", "block_placa_tol",
            "fuzzy_min_len", "spatial_cluster_eps_m",
            "spatial_bridge_max_m"} <= umbrales
    for method in config.METHOD_TRUST_BASE:
        assert f"trust_base.{method}" in umbrales


def test_stats_carry_run_provenance(client, sheets, df_consolidada, match_table,
                                    report):
    from datetime import datetime, timezone
    _, stats, _ = sheets
    meta = export_sheets.run_info(datetime.now(timezone.utc), with_embedding=True,
                                  bridge_info={"mode": "surrogate"})

    push_to_sheets(df_consolidada, match_table, report, client=client, run_meta=meta)

    ejecucion = {r[1]: r[2] for r in stats.written if r and r[0] == "ejecucion"}
    assert {"timestamp_utc", "timestamp_bogota", "duracion_seg", "origen",
            "bridge_modo", "version"} <= set(ejecucion)
    assert ejecucion["bridge_modo"] == "surrogate"


def test_stats_break_trust_down_by_method(client, sheets, df_consolidada,
                                          match_table, report):
    _, stats, _ = sheets
    push_to_sheets(df_consolidada, match_table, report, client=client)

    per_method = {r[1] for r in stats.written if r and r[0] == "trust_por_metodo"}
    assert {"handshake.n", "handshake.media", "handshake.min"} <= per_method


# ── Chunking ──────────────────────────────────────────────────────────────────
def test_large_payloads_are_split_into_sequential_top_down_writes():
    values = [[f"row-{i}", "x" * 200] for i in range(100)]
    chunks = list(export_sheets._chunks(values, max_chars=1_000))

    assert len(chunks) > 1
    offsets = [offset for offset, _ in chunks]
    assert offsets == sorted(offsets)
    assert sum(len(c) for _, c in chunks) == len(values)
    assert [r for _, c in chunks for r in c] == values
