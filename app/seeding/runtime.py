"""Runtime capability checks that never disclose secret contents."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict


def _file_capability(environment_name: str) -> Dict[str, Any]:
    value = os.getenv(environment_name, "").strip()
    if not value:
        return {"configured": False, "readable": False}
    path = Path(value)
    return {
        "configured": True,
        "readable": path.is_file() and os.access(path, os.R_OK),
    }


def runtime_capabilities(
    *,
    authenticator_mounted: bool,
    juguang_read_mounted: bool,
    execution_mounted: bool,
    production_create_mounted: bool,
    release_mounted: bool,
    grant_signer_mounted: bool,
) -> Dict[str, Any]:
    auth_file = _file_capability("SEEDING_OPERATOR_AUTH_FILE")
    auth_json_configured = bool(os.getenv("SEEDING_OPERATOR_AUTH_JSON", "").strip())
    token_file = _file_capability("JUGUANG_ACCESS_TOKEN_FILE")
    signing_file = _file_capability("SEEDING_GRANT_SIGNING_KEY_FILE")
    return {
        "planning": {
            "ready": authenticator_mounted,
            "authentication_configured": auth_file["readable"]
            or auth_json_configured
            or authenticator_mounted,
        },
        "juguang_read": {
            "ready": juguang_read_mounted,
            "token_file": token_file,
            "adapter_mounted": juguang_read_mounted,
        },
        "test_execution": {
            "ready": execution_mounted and grant_signer_mounted,
            "adapter_mounted": execution_mounted,
            "grant_signer_mounted": grant_signer_mounted,
            "signing_key_file": signing_file,
        },
        "production_paused_create": {
            "ready": production_create_mounted and grant_signer_mounted,
            "adapter_mounted": production_create_mounted,
        },
        "production_release": {
            "ready": release_mounted and grant_signer_mounted,
            "adapter_mounted": release_mounted,
        },
    }
