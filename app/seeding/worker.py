"""Lease-backed worker for durable SEEDING jobs."""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .service import SeedingService

LOGGER = logging.getLogger(__name__)


class DurableWorker:
    def __init__(
        self,
        service: SeedingService,
        *,
        worker_id: str,
        poll_seconds: float = 0.5,
    ) -> None:
        self.service = service
        self.worker_id = worker_id
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"seeding-worker-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.service.process_next_job(worker_id=self.worker_id)
                if result is None:
                    self._stop.wait(self.poll_seconds)
            except Exception:
                LOGGER.exception("SEEDING durable job failed")
                self._stop.wait(self.poll_seconds)
