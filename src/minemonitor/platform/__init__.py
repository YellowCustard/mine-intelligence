"""Platform foundation (Phase 1): the extension substrate for future domains.

This package is scaffolding, not a new domain. It provides three seams that the
advanced-platform capabilities (vision, maintenance, fuel, weighbridge, dispatch)
will build on, without changing any existing behaviour:

- ``contracts`` — a registry of versioned event contracts (schema string → model).
- ``bus`` — a lightweight in-process publish/subscribe event bus.
- ``metrics`` — process-local counters for observability.

The existing ingest/pipeline hot path is deliberately **not** rewired through the
bus yet: that is a later, evidence-driven migration step. Phase 1 only makes the
seam available (see docs/PLATFORM_EVOLUTION_ARCHITECTURE.md §5).
"""
