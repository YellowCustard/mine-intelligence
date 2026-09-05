"""Haul-cycle analytics — the commercial payload.

A cycle is one complete traversal back to the load zone. Segment durations come
from zone transitions; the headline metric is **queue time at the face**. Cycles
are recomputable from stored positions, so a fix to the state machine can be
applied retrospectively (brief §9). No target or benchmark is ever hardcoded —
we report this mine's real numbers.
"""

from minemonitor.cycles.statemachine import Cycle, compute_cycles

__all__ = ["Cycle", "compute_cycles"]
