"""Standalone entry point for the isolated SEEDING API.

Authentication is deny-all unless SEEDING_OPERATOR_AUTH_JSON is supplied by a
secret manager.  The value is never logged or persisted by this module.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.seeding.api import authenticator_from_environment, create_seeding_app
from app.seeding.alerts import WebhookAlertSink
from app.seeding.grants import GrantSigner
from app.seeding.juguang import JuguangClient, token_provider_from_environment

DATABASE_PATH = Path(
    os.getenv(
        "SEEDING_DATABASE_PATH",
        str(Path(__file__).resolve().parent / "runtime" / "seeding.sqlite3"),
    )
)

GRANT_SIGNER = (
    GrantSigner.from_environment()
    if os.getenv("SEEDING_GRANT_SIGNING_KEY_FILE", "").strip()
    else None
)

JUGUANG_READ_CLIENT = (
    JuguangClient(token_provider=token_provider_from_environment())
    if os.getenv("JUGUANG_ACCESS_TOKEN_FILE", "").strip()
    else None
)

ALERT_SINK = (
    WebhookAlertSink.from_environment()
    if os.getenv("SEEDING_ALERT_WEBHOOK_FILE", "").strip()
    else None
)

app = create_seeding_app(
    database_path=DATABASE_PATH,
    authenticator=authenticator_from_environment(),
    grant_signer=GRANT_SIGNER,
    juguang_read_client=JUGUANG_READ_CLIENT,
    alert_sink=ALERT_SINK,
)
