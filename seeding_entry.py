"""Standalone entry point for the isolated SEEDING API.

Authentication is deny-all unless SEEDING_OPERATOR_AUTH_JSON is supplied by a
secret manager.  The value is never logged or persisted by this module.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.seeding.api import authenticator_from_environment, create_seeding_app

DATABASE_PATH = Path(
    os.getenv(
        "SEEDING_DATABASE_PATH",
        str(Path(__file__).resolve().parent / "runtime" / "seeding.sqlite3"),
    )
)

app = create_seeding_app(
    database_path=DATABASE_PATH,
    authenticator=authenticator_from_environment(),
)
