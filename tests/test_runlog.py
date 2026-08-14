"""Guards for the durable logging used by the scheduled job.

Logging must never be the reason a run fails, and the history it keeps has to
survive rotation.
"""
import json
import sys

from integracion import runlog


def test_resolve_log_dir_creates_the_directory(tmp_path):
    target = tmp_path / "logs"
    assert runlog.resolve_log_dir(str(target)) == target
    assert target.is_dir()


def test_resolve_log_dir_falls_back_when_nothing_is_writable(tmp_path, monkeypatch):
    """A container without the volume mounted must still run — stdout only."""
    monkeypatch.delenv("LOG_DIR", raising=False)
    monkeypatch.setattr(runlog, "DEFAULT_LOG_DIR", str(tmp_path / "x" / "\0bad"))
    assert runlog.resolve_log_dir(str(tmp_path / "y" / "\0bad")) is None


def test_resolve_log_dir_honours_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "from-env"))
    assert runlog.resolve_log_dir() == tmp_path / "from-env"


def test_tee_writes_to_both_console_and_file(tmp_path, capsys):
    log_dir = runlog.resolve_log_dir(str(tmp_path))
    restore = runlog.start_tee(log_dir)
    try:
        print("hola mundo")
        print("segunda linea", file=sys.stderr)
    finally:
        restore()

    captured = capsys.readouterr()
    assert "hola mundo" in captured.out
    assert "segunda linea" in captured.err

    written = (log_dir / runlog.LOG_FILE).read_text(encoding="utf-8")
    assert "hola mundo" in written and "segunda linea" in written


def test_tee_restores_the_original_streams(tmp_path):
    original_out, original_err = sys.stdout, sys.stderr
    restore = runlog.start_tee(runlog.resolve_log_dir(str(tmp_path)))
    assert sys.stdout is not original_out
    restore()
    assert sys.stdout is original_out and sys.stderr is original_err


def test_tee_is_a_noop_without_a_log_dir(capsys):
    restore = runlog.start_tee(None)
    print("sin volumen")
    restore()
    assert "sin volumen" in capsys.readouterr().out


def test_log_file_rotates_and_keeps_backups(tmp_path):
    writer = runlog._LineWriter(tmp_path / "integracion.log", max_bytes=200,
                                backup_count=3)
    try:
        for i in range(200):
            writer.emit_text(f"linea de relleno numero {i}")
    finally:
        writer.close()

    assert (tmp_path / "integracion.log").exists()
    assert (tmp_path / "integracion.log.1").exists()
    assert (tmp_path / "integracion.log").stat().st_size <= 400


def test_run_history_appends_one_line_per_execution(tmp_path):
    log_dir = runlog.resolve_log_dir(str(tmp_path))
    runlog.append_run(log_dir, {"estado": "ok", "tasa_match_pct": 62.0})
    runlog.append_run(log_dir, {"estado": "error", "error": "boom"})

    lines = (log_dir / runlog.RUNS_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["estado"] == "ok" and first["tasa_match_pct"] == 62.0
    assert "timestamp_utc" in first and "timestamp_bogota" in first


def test_run_history_is_readable_back(tmp_path):
    log_dir = runlog.resolve_log_dir(str(tmp_path))
    for i in range(30):
        runlog.append_run(log_dir, {"estado": "ok", "n": i})

    recent = runlog.read_runs(log_dir, limit=5)
    assert [r["n"] for r in recent] == [25, 26, 27, 28, 29]


def test_run_history_survives_a_corrupt_line(tmp_path):
    log_dir = runlog.resolve_log_dir(str(tmp_path))
    runlog.append_run(log_dir, {"estado": "ok"})
    with open(log_dir / runlog.RUNS_FILE, "a", encoding="utf-8") as fh:
        fh.write("{no es json\n")
    runlog.append_run(log_dir, {"estado": "ok"})

    assert len(runlog.read_runs(log_dir)) == 2


def test_append_run_without_a_log_dir_does_nothing():
    runlog.append_run(None, {"estado": "ok"})
    assert runlog.read_runs(None) == []
