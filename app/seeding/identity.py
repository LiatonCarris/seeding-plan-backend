"""Canonical hashing and stable plan identity helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from pydantic import BaseModel


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def logical_plan_key(
    *,
    account_id: str,
    project_id: str,
    stage: str,
    channel: str,
    note_id: str,
    objective: str,
    audience_id: Optional[str] = None,
    primary_lane: Optional[str] = None,
) -> str:
    """Build stable identity without config_sha or an execution grant."""

    if (audience_id is None) == (primary_lane is None):
        raise ValueError("exactly one of audience_id or primary_lane is required")
    payload: Dict[str, str] = {
        "account_id": account_id,
        "project_id": project_id,
        "stage": stage,
        "channel": channel,
        "note_id": note_id,
        "objective": objective,
    }
    if audience_id is not None:
        payload["audience_id"] = audience_id
    if primary_lane is not None:
        payload["primary_lane"] = primary_lane
    return sha256_json(payload)
