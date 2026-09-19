"""Laboratory data ingestion (FP-08): direct assay ingest with an immutable audit trail.

The measured result is stored write-once and hashed (tamper-evidence); corrections are
append-only annotations that reference a result without mutating it, carrying the actor and
reason. Anomalous values are **flagged** against configured bounds (an advisory ``event.v1``),
never silently altered. This is the audit backbone gold reconciliation (FP-09) reads from.
"""
