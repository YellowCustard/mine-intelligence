"""Versioned event-contract registry (Phase 1 foundation).

The platform's spine is that every source speaks a versioned ``*.v1`` event. This
registry is the single place those contracts are declared, so the event bus and any
future domain can validate a payload against its schema by name and enumerate what
the platform understands. It references the existing Pydantic contracts rather than
duplicating them; new domains (vision, maintenance, fuel, weighbridge, dispatch)
register their models here as they are built.

Backward compatibility rule: a published schema version is immutable. A breaking
change ships as a new version (``foo.v2``) registered alongside ``foo.v1``.
"""

from __future__ import annotations

from pydantic import BaseModel

from minemonitor.contracts import AssetMetricsV1, AssetPositionV1, EventV1
from minemonitor.contracts.dispatch import DispatchRecommendationV1
from minemonitor.contracts.fuel import FuelTransactionV1
from minemonitor.contracts.maintenance import MaintenanceHealthV1
from minemonitor.contracts.weighbridge import WeighbridgeTransactionV1

# The registered contracts, keyed by their schema string. Extend this as new domains
# land — never mutate an existing entry's shape (add a new version instead).
_REGISTRY: dict[str, type[BaseModel]] = {
    "asset.position.v1": AssetPositionV1,
    "event.v1": EventV1,
    "asset.metrics.v1": AssetMetricsV1,
    "fuel.transaction.v1": FuelTransactionV1,
    "weighbridge.transaction.v1": WeighbridgeTransactionV1,
    "maintenance.health.v1": MaintenanceHealthV1,
    "dispatch.recommendation.v1": DispatchRecommendationV1,
}


def register(schema: str, model: type[BaseModel]) -> None:
    """Register (or confirm) a contract. Re-registering the same model is a no-op;
    rebinding a schema string to a different model is refused (versions are immutable).
    """
    existing = _REGISTRY.get(schema)
    if existing is not None and existing is not model:
        raise ValueError(f"schema {schema!r} already registered to {existing.__name__}")
    _REGISTRY[schema] = model


def model_for(schema: str) -> type[BaseModel] | None:
    return _REGISTRY.get(schema)


def registered() -> list[str]:
    """All known schema strings, sorted."""
    return sorted(_REGISTRY)


def validate(schema: str, payload: dict) -> BaseModel:
    """Validate a payload dict against its registered contract. Raises if unknown."""
    model = _REGISTRY.get(schema)
    if model is None:
        raise KeyError(f"unknown schema {schema!r}")
    return model.model_validate(payload)
