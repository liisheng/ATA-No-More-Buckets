import pytest

from app.models import IncidentStatus
from app.state_machine import InvalidTransition, validate_transition


def test_happy_path_transitions_are_explicit() -> None:
    current = IncidentStatus.REPORTED
    for target in [
        IncidentStatus.TRIAGED,
        IncidentStatus.CONTAINED,
        IncidentStatus.DISPATCHING,
        IncidentStatus.SCHEDULED,
        IncidentStatus.IN_PROGRESS,
        IncidentStatus.VERIFYING,
        IncidentStatus.PROVISIONALLY_RESOLVED,
        IncidentStatus.CLOSED,
    ]:
        transition = validate_transition(current, target, "TEST_RULE")
        assert transition.to_status == target
        current = target


def test_invalid_transition_is_rejected() -> None:
    with pytest.raises(InvalidTransition):
        validate_transition(IncidentStatus.REPORTED, IncidentStatus.CLOSED, "BAD_RULE")
