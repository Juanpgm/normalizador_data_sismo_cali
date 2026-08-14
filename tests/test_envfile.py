"""The .env loader: local secrets reach os.environ without ever touching git."""
from integracion.envfile import load_env_file


def _write(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_key_value_pairs(tmp_path):
    env = {}
    load_env_file(_write(tmp_path, "GOOGLE_MAPS_API_KEY=AIzaFake123\nOTHER=x\n"),
                  environ=env)
    assert env["GOOGLE_MAPS_API_KEY"] == "AIzaFake123"
    assert env["OTHER"] == "x"


def test_real_environment_wins_over_the_file():
    """A variable set in the shell (or Railway) must never be clobbered."""
    env = {"GOOGLE_MAPS_API_KEY": "from-shell"}
    import io, tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / ".env"
        p.write_text("GOOGLE_MAPS_API_KEY=from-file\n", encoding="utf-8")
        load_env_file(p, environ=env)
    assert env["GOOGLE_MAPS_API_KEY"] == "from-shell"


def test_ignores_comments_blanks_and_strips_quotes(tmp_path):
    env = {}
    load_env_file(_write(tmp_path, (
        "# secrets live here\n"
        "\n"
        'QUOTED="hello world"\n'
        "SINGLE='abc'\n"
        "  SPACED = padded  \n"
        "not a valid line\n"
    )), environ=env)
    assert env["QUOTED"] == "hello world"
    assert env["SINGLE"] == "abc"
    assert env["SPACED"] == "padded"
    assert "not a valid line" not in env


def test_blank_values_are_skipped(tmp_path):
    """The placeholder `KEY=` in the template must not mask a missing secret."""
    env = {}
    load_env_file(_write(tmp_path, "GOOGLE_MAPS_API_KEY=\n"), environ=env)
    assert "GOOGLE_MAPS_API_KEY" not in env


def test_missing_file_is_a_noop(tmp_path):
    env = {}
    load_env_file(tmp_path / "does-not-exist.env", environ=env)
    assert env == {}
