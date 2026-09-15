"""Dispatch domain (Phase 6): decision-support.

The first implementation is decision-support, not machine autonomy: the platform
produces **advisory** truck→job recommendations with explicit evidence, a supervisor
approves them, and only then are they dispatched as information. Nothing here actuates
a machine (brief §15). The recommendation heuristic is transparent and prioritised by
job priority and truck availability — it is never presented as an optimal solution.
Optimisation objectives beyond that are later increments.
"""
