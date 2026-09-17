"""SEEDING planning and fail-closed Juguang execution branch.

The package is deliberately isolated from the frozen ROI/CID contracts.
Platform writes require explicit signed grants plus injected relock and spend
safety adapters; the default application entry remains write-disabled.
"""

from .contracts import ProjectConfig
from .service import SeedingService
from .store import SeedingStore

__all__ = ["ProjectConfig", "SeedingService", "SeedingStore"]
