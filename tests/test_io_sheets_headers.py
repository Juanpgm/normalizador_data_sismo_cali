"""Resilience of the sheet reader against header edits in the live document."""
import pandas as pd

from integracion.io_sheets import recover_barrio_header


def _sheet(barrio_header):
    return pd.DataFrame(
        [["1", "Alta", "Zona 1", "El Lido", "Casa"]],
        columns=["ID", "Prioridad", "Zona", barrio_header, "Tipo"])


def test_recovers_a_blanked_barrio_header():
    out = recover_barrio_header(_sheet("-"))
    assert "Barrio" in out.columns
    assert out["Barrio"].iloc[0] == "El Lido"


def test_recovers_an_empty_barrio_header():
    assert "Barrio" in recover_barrio_header(_sheet("")).columns


def test_leaves_a_healthy_sheet_alone():
    out = recover_barrio_header(_sheet("Barrio"))
    assert list(out.columns) == ["ID", "Prioridad", "Zona", "Barrio", "Tipo"]


def test_does_not_rename_a_named_column():
    """Only an unnamed column is recoverable — never one that says something."""
    out = recover_barrio_header(_sheet("Observaciones"))
    assert "Barrio" not in out.columns


def test_does_not_guess_when_the_anchors_are_not_adjacent():
    df = pd.DataFrame([["Zona 1", "-", "x", "Casa"]],
                      columns=["Zona", "-", "otra", "Tipo"])
    assert "Barrio" not in recover_barrio_header(df).columns


def test_does_not_guess_without_anchors():
    df = pd.DataFrame([["a", "b"]], columns=["ID", "-"])
    assert "Barrio" not in recover_barrio_header(df).columns
