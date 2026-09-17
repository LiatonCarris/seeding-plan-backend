"""Durable alert delivery backed by the SQLite outbox."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Protocol

import httpx

from .errors import SeedingError
from .store import SeedingStore


class AlertSink(Protocol):
    def send(self, event: Mapping[str, Any]) -> None: ...


class WebhookAlertSink:
    def __init__(self, *, url: str, timeout_seconds: float = 10.0) -> None:
        if not url.startswith("https://"):
            raise ValueError("alert webhook must use HTTPS")
        self._url = url
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "WebhookAlertSink":
        filename = os.getenv("SEEDING_ALERT_WEBHOOK_FILE", "").strip()
        if not filename:
            raise SeedingError(
                "ALERT_WEBHOOK_MISSING",
                "SEEDING_ALERT_WEBHOOK_FILE is not configured",
                status_code=503,
            )
        try:
            url = Path(filename).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SeedingError(
                "ALERT_WEBHOOK_UNAVAILABLE",
                "alert webhook file cannot be read",
                status_code=503,
            ) from exc
        return cls(url=url)

    def send(self, event: Mapping[str, Any]) -> None:
        try:
            response = httpx.post(
                self._url,
                json={
                    "event_type": event["event_type"],
                    "project_id": event["project_id"],
                    "payload": event["payload"],
                    "created_at": event["created_at"],
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SeedingError(
                "ALERT_DELIVERY_FAILED",
                "alert webhook delivery failed",
                status_code=502,
            ) from exc


def is_alert_event(event: Mapping[str, Any]) -> bool:
    event_type = event["event_type"]
    payload = event["payload"]
    if event_type == "ALERT":
        return True
    if event_type == "EXECUTION_STATE_CHANGED":
        return payload.get("to") in {
            "PAUSE_FAILED_EMERGENCY",
            "READBACK_MISMATCH",
            "RECONCILE_REQUIRED",
            "RECOVERY_REQUIRED",
            "RELOCK_FAILED",
        }
    if event_type == "DELIVERY_STATE_CHANGED":
        return payload.get("to") in {"CLOSING", "CLOSED"}
    return False


class AlertDispatcher:
    def __init__(self, store: SeedingStore, sink: AlertSink, *, worker_id: str) -> None:
        self.store = store
        self.sink = sink
        self.worker_id = worker_id

    def flush(self, *, limit: int = 50) -> int:
        delivered = 0
        for event in self.store.claim_outbox(worker_id=self.worker_id, limit=limit):
            try:
                if is_alert_event(event):
                    self.sink.send(event)
                    delivered += 1
                self.store.complete_outbox(event["event_id"], worker_id=self.worker_id)
            except Exception as exc:
                self.store.fail_outbox(
                    event["event_id"],
                    worker_id=self.worker_id,
                    error=str(exc),
                )
        return delivered
