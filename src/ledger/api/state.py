"""Process-wide resources, built once in the app lifespan.

Held on ``app.state`` rather than in module globals so tests can construct one
directly and so nothing can reach a resource without being handed it.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiokafka import AIOKafkaProducer

from ledger.catalog.models import Catalog
from ledger.config import Settings
from ledger.engine.duck import Engine
from ledger.governance.publisher import AuditPublisher


@dataclass(slots=True)
class AppState:
    settings: Settings
    engine: Engine
    catalog: Catalog
    publisher: AuditPublisher
    producer: AIOKafkaProducer
