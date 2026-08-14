"""Shared fixtures. Adds the repo root to sys.path and runs the pipeline once
(cached, no LM tier) for the progress tests. Skips cleanly if data is
unavailable (no cache and no Google credentials)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def artifacts():
    from integracion.pipeline import run
    try:
        return run(use_cache=True, with_embedding=False, with_bridge=True, export=False)
    except Exception as e:  # no cache + no credentials, or offline
        pytest.skip(f"pipeline data unavailable: {type(e).__name__}: {e}")
