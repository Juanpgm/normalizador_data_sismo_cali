"""Single source of Google service-account credentials.

Two delivery mechanisms, in priority order:

1. ``GOOGLE_SERVICE_ACCOUNT_JSON`` — the whole key file as an environment
   variable. This is how CI (GitHub Actions secret) and any container runtime
   provide the credential: nothing is ever written to disk.
2. ``service_account.json`` at the repo root — the local development path.

The key file is gitignored and must never be committed.
"""
from __future__ import annotations

import json
import os

from google.oauth2.service_account import Credentials

from .config import SERVICE_ACCOUNT_FILE

ENV_VAR = "GOOGLE_SERVICE_ACCOUNT_JSON"


def credentials(scopes) -> Credentials:
    """Build service-account credentials for ``scopes``.

    Raises RuntimeError with an actionable message when neither the environment
    variable nor the local key file is available.
    """
    raw = os.environ.get(ENV_VAR, "").strip()
    if raw:
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{ENV_VAR} is set but is not valid JSON: {exc}"
            ) from exc
        return Credentials.from_service_account_info(info, scopes=list(scopes))

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise RuntimeError(
            f"No Google credentials found. Set {ENV_VAR} with the service-account "
            f"key, or place the key file at {SERVICE_ACCOUNT_FILE}."
        )
    return Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE,
                                                 scopes=list(scopes))


def service_account_email() -> str:
    """Email of the active service account — useful in logs and error messages."""
    raw = os.environ.get(ENV_VAR, "").strip()
    info = json.loads(raw) if raw else json.load(open(SERVICE_ACCOUNT_FILE, encoding="utf-8"))
    return info.get("client_email", "<unknown>")
