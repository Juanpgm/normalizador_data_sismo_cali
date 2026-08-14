"""Tiny .env loader — local secrets without a dependency and without git risk.

The repo's .gitignore excludes `.env`, so the API key lives there on a dev
machine and is read into the process environment at import time (config.py
calls this). Two rules keep it safe and predictable:

* a variable already present in the real environment ALWAYS wins — Railway and
  CI set variables directly and must never be shadowed by a stale local file;
* blank values are skipped, so the committed-nowhere placeholder `KEY=` never
  masks a genuinely missing secret.

Deliberately minimal (KEY=VALUE, comments, optional quotes) instead of pulling
in python-dotenv for one file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')


def load_env_file(path, environ=None) -> int:
    """Load KEY=VALUE pairs from ``path`` into ``environ``. Returns how many
    variables were actually set. Missing file is a silent no-op."""
    environ = os.environ if environ is None else environ
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return 0
    loaded = 0
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not value or key in environ:
            continue
        environ[key] = value
        loaded += 1
    return loaded
