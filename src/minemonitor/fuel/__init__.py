"""Fuel domain (Phase 3): measured transactions, tank readings, reconciliation.

Measured facts (litres, tank levels, odometer/engine-hour readings) are stored as
immutable observations. Anything derived (consumption efficiency) is computed on read
and explicitly labelled measured vs calculated — never presented as a measured fact.
Reconciliation flags discrepancies; it never silently corrects them.
"""
