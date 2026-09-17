from __future__ import annotations

from seeding_fixtures import project_config

from app.seeding.alerts import AlertDispatcher
from app.seeding.store import SeedingStore


class MemorySink:
    def __init__(self) -> None:
        self.events = []

    def send(self, event) -> None:
        self.events.append(event)


def test_alert_dispatcher_uses_durable_outbox_and_filters_non_alert_events(
    tmp_path,
) -> None:
    store = SeedingStore(tmp_path / "seeding.sqlite3")
    config = project_config()
    store.create_project(config, owner_principal_id="owner-1")
    store.emit_event(
        project_id=config.project_id,
        event_type="ALERT",
        payload={"severity": "CRITICAL", "code": "RELOCK_FAILED"},
    )
    sink = MemorySink()
    delivered = AlertDispatcher(store, sink, worker_id="alert-worker").flush()
    assert delivered == 1
    assert sink.events[0]["payload"]["code"] == "RELOCK_FAILED"
    audit = store.audit_trail(config.project_id)
    assert all(item["status"] == "PUBLISHED" for item in audit["outbox_events"])
