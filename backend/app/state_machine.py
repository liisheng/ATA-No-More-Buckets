from __future__ import annotations

from dataclasses import dataclass

from .models import IncidentStatus

ALLOWED_TRANSITIONS: dict[IncidentStatus, set[IncidentStatus]] = {
    IncidentStatus.REPORTED: {
        IncidentStatus.TRIAGED,
        IncidentStatus.ESCALATED,
        IncidentStatus.CANCELLED,
    },
    IncidentStatus.TRIAGED: {
        IncidentStatus.CONTAINED,
        IncidentStatus.ESCALATED,
        IncidentStatus.CANCELLED,
    },
    IncidentStatus.CONTAINED: {
        IncidentStatus.DISPATCHING,
        IncidentStatus.ESCALATED,
        IncidentStatus.CANCELLED,
    },
    IncidentStatus.DISPATCHING: {
        IncidentStatus.SCHEDULED,
        IncidentStatus.ESCALATED,
        IncidentStatus.CANCELLED,
    },
    IncidentStatus.SCHEDULED: {
        IncidentStatus.IN_PROGRESS,
        IncidentStatus.ESCALATED,
        IncidentStatus.CANCELLED,
    },
    IncidentStatus.IN_PROGRESS: {
        IncidentStatus.VERIFYING,
        IncidentStatus.ESCALATED,
        IncidentStatus.CANCELLED,
    },
    IncidentStatus.VERIFYING: {
        IncidentStatus.PROVISIONALLY_RESOLVED,
        IncidentStatus.ESCALATED,
        IncidentStatus.REOPENED,
    },
    IncidentStatus.PROVISIONALLY_RESOLVED: {
        IncidentStatus.CLOSED,
        IncidentStatus.REOPENED,
        IncidentStatus.ESCALATED,
    },
    IncidentStatus.CLOSED: {IncidentStatus.REOPENED},
    IncidentStatus.ESCALATED: {
        IncidentStatus.TRIAGED,
        IncidentStatus.CONTAINED,
        IncidentStatus.DISPATCHING,
        IncidentStatus.SCHEDULED,
        IncidentStatus.VERIFYING,
        IncidentStatus.CANCELLED,
    },
    IncidentStatus.CANCELLED: set(),
    IncidentStatus.REOPENED: {
        IncidentStatus.TRIAGED,
        IncidentStatus.CONTAINED,
        IncidentStatus.ESCALATED,
        IncidentStatus.CANCELLED,
    },
}


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True)
class StateTransition:
    from_status: IncidentStatus
    to_status: IncidentStatus
    rule_id: str


def validate_transition(
    current: IncidentStatus, target: IncidentStatus, rule_id: str
) -> StateTransition:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"{current} -> {target} rejected by {rule_id}")
    return StateTransition(current, target, rule_id)
