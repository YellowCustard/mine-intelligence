"""Maintenance domain (Phase 5): deterministic health indicators.

Increment 1 is deterministic only — risk is derived from a service interval and the
time/hours since the last completed service, with explicit basis, confidence and
evidence. No predictive model, no invented sensor values, and the platform never
autonomously stops a machine (advisory, brief §15). Anomaly detection and predictive
models are later increments, gated on real historical data.
"""
