from __future__ import annotations

from collections.abc import Mapping


class InvalidTransition(ValueError):
    """Raised when a governed workflow attempts an illegal state change."""


_TRANSITIONS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "sample": {
        "Draft": frozenset({"Designing", "Cancelled"}),
        "Designing": frozenset({"Sampling", "Cancelled"}),
        "Sampling": frozenset({"Sent", "Cancelled"}),
        "Sent": frozenset({"Feedback", "Cancelled"}),
        "Feedback": frozenset({"Approved", "Rejected", "Cancelled"}),
        "Approved": frozenset(),
        "Rejected": frozenset(),
        "Cancelled": frozenset(),
    },
    "demand": {
        "Draft": frozenset({"Confirmed", "Cancelled"}),
        "Confirmed": frozenset({"Sourcing", "Cancelled"}),
        "Sourcing": frozenset({"Fulfilled", "Cancelled"}),
        "Fulfilled": frozenset(),
        "Cancelled": frozenset(),
    },
    "sourcing": {
        "Draft": frozenset({"Invited", "Cancelled"}),
        "Invited": frozenset({"Collecting", "Cancelled"}),
        "Collecting": frozenset({"Evaluating", "Cancelled"}),
        "Evaluating": frozenset({"Selected", "Closed", "Cancelled"}),
        "Selected": frozenset({"Closed"}),
        "Closed": frozenset(),
        "Cancelled": frozenset(),
    },
    "work": {
        "Open": frozenset({"In Progress", "Blocked", "Cancelled"}),
        "In Progress": frozenset({"Blocked", "Done", "Cancelled"}),
        "Blocked": frozenset({"In Progress", "Cancelled"}),
        "Done": frozenset(),
        "Cancelled": frozenset(),
    },
    "review": {
        "Pending": frozenset({"Approved", "Rejected", "Superseded"}),
        "Approved": frozenset(),
        "Rejected": frozenset(),
        "Superseded": frozenset(),
    },
}

_INITIAL_STATUSES: Mapping[str, str] = {
    "sample": "Draft",
    "demand": "Draft",
    "sourcing": "Draft",
    "work": "Open",
    "review": "Pending",
}


def validate_initial_status(workflow: str, status: str) -> None:
    try:
        initial = _INITIAL_STATUSES[workflow]
    except KeyError as error:
        raise InvalidTransition(f"unknown workflow: {workflow}") from error
    if status != initial:
        raise InvalidTransition(f"invalid {workflow} initial status: {status}; expected {initial}")


def transitions_for(workflow: str, status: str) -> frozenset[str]:
    try:
        return _TRANSITIONS[workflow][status]
    except KeyError as error:
        raise InvalidTransition(f"unknown {workflow} status: {status}") from error


def validate_transition(workflow: str, before: str, after: str) -> None:
    if before == after:
        return
    if after not in transitions_for(workflow, before):
        raise InvalidTransition(f"illegal {workflow} transition: {before} -> {after}")
