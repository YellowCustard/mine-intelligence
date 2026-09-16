"""Weighbridge domain (Phase 4): measured tickets, tonnage, reconciliation, adapters.

Manufacturer-neutral. Gross/tare/net are measured facts stored as observations;
net-consistency and (later) dispatch/stock reconciliation *flag* discrepancies rather
than correcting them. Tickets are idempotent per (site, ticket_no) so a CSV re-import
or a retried API call never double-counts production.
"""
