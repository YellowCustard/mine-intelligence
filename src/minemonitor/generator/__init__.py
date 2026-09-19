"""Generator monitoring (FP-11): measured DSE genset readings → maintenance health + alarms.

Readings are measured controller data (engine hours, running state, fuel, faults), stored
write-once and idempotent. The latest engine hours feed the existing deterministic maintenance
health; a fault or an unexpected run raises an advisory ``event.v1``. Advisory only — the
platform monitors and alerts, it never controls a genset. Nothing is fabricated where the
controller does not report it.
"""
