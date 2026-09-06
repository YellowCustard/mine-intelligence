"""The incident state machine — pure lifecycle rules, no database."""

from __future__ import annotations

from minemonitor.operations import incidents
from minemonitor.operations.incidents import can_transition


def test_forward_transitions_are_allowed() -> None:
    assert can_transition("open", "acknowledged")
    assert can_transition("acknowledged", "investigating")
    assert can_transition("investigating", "assigned")
    assert can_transition("assigned", "resolved")
    assert can_transition("resolved", "closed")


def test_may_skip_intermediate_states() -> None:
    # Acknowledging is optional — you can resolve a trivial alarm directly.
    assert can_transition("open", "resolved")
    assert can_transition("open", "closed")


def test_closed_is_terminal() -> None:
    for state in incidents.INCIDENT_STATES:
        assert not can_transition("closed", state)


def test_resolved_can_reopen_but_not_jump_backwards_arbitrarily() -> None:
    assert can_transition("resolved", "investigating")  # the fix did not hold
    assert not can_transition("resolved", "acknowledged")


def test_unknown_states_never_transition() -> None:
    assert not can_transition("nonsense", "open")
    assert not can_transition("open", "nonsense")


def test_all_targets_are_known_states() -> None:
    known = set(incidents.INCIDENT_STATES)
    for source, targets in incidents._ALLOWED.items():
        assert source in known
        assert targets <= known
