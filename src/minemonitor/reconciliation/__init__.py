"""Gold reconciliation (FP-09): measurement ingestion groundwork.

Records measured quantities through the gold circuit (ore→crusher→mill→tanks→carbon→furnace→
output→gate) with source provenance, so the later expected-vs-observed reconciliation slices are
reproducible from stored measurements. This groundwork ingests only measured inputs; it never
computes an expectation, a variance, or a theft conclusion — that boundary (flag, a human
concludes) lives in the deferred reconciliation engine.
"""
