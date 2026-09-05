"""The device adapter contract.

Every adapter — the simulator now, a Teltonika TCP codec later — turns its
device's raw feed into a stream of ``PositionIngest`` payloads that are, at the
ingest boundary, indistinguishable from one another (brief §10). Whatever a real
tracker cannot express here is a gap in the abstraction, not a special case.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from minemonitor.ingest.service import PositionIngest


class DeviceAdapter(ABC):
    """Produces normalised position payloads from a device or protocol."""

    #: Names the sensing modality, e.g. ``"simulator"`` or ``"teltonika:fmb920"``.
    source: str

    @abstractmethod
    def positions(self) -> Iterator[PositionIngest]:
        """Yield position payloads. May be finite (a replay) or unbounded (live)."""
        raise NotImplementedError
