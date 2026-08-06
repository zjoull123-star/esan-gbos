from __future__ import annotations

import pytest
from esan_gbos.domain.state_machine import (
    InvalidTransition,
    validate_initial_status,
    validate_transition,
)


@pytest.mark.parametrize(
    ("workflow", "before", "after"),
    [
        ("sample", "Draft", "Designing"),
        ("sample", "Designing", "Sampling"),
        ("sample", "Sampling", "Sent"),
        ("sample", "Sent", "Feedback"),
        ("sample", "Feedback", "Approved"),
        ("sample", "Feedback", "Rejected"),
        ("demand", "Draft", "Confirmed"),
        ("demand", "Confirmed", "Sourcing"),
        ("demand", "Sourcing", "Fulfilled"),
        ("sourcing", "Draft", "Invited"),
        ("sourcing", "Invited", "Collecting"),
        ("sourcing", "Collecting", "Evaluating"),
        ("sourcing", "Evaluating", "Selected"),
        ("work", "Open", "In Progress"),
        ("work", "In Progress", "Blocked"),
        ("work", "Blocked", "In Progress"),
        ("work", "In Progress", "Done"),
        ("review", "Pending", "Approved"),
        ("review", "Pending", "Rejected"),
        ("review", "Pending", "Superseded"),
    ],
)
def test_accepts_allowed_transition(workflow: str, before: str, after: str) -> None:
    validate_transition(workflow, before, after)


@pytest.mark.parametrize(
    ("workflow", "before", "after"),
    [
        ("sample", "Draft", "Approved"),
        ("sample", "Approved", "Sampling"),
        ("demand", "Draft", "Fulfilled"),
        ("sourcing", "Invited", "Selected"),
        ("work", "Open", "Done"),
        ("review", "Approved", "Pending"),
    ],
)
def test_rejects_illegal_transition(workflow: str, before: str, after: str) -> None:
    with pytest.raises(InvalidTransition, match=f"{before} -> {after}"):
        validate_transition(workflow, before, after)


def test_all_active_workflows_allow_cancelled_from_nonterminal_states() -> None:
    for workflow, before in (
        ("sample", "Designing"),
        ("demand", "Confirmed"),
        ("sourcing", "Collecting"),
        ("work", "Blocked"),
    ):
        validate_transition(workflow, before, "Cancelled")


@pytest.mark.parametrize(
    ("workflow", "initial"),
    (
        ("sample", "Draft"),
        ("demand", "Draft"),
        ("sourcing", "Draft"),
        ("work", "Open"),
        ("review", "Pending"),
    ),
)
def test_workflows_require_their_initial_state_on_create(
    workflow: str,
    initial: str,
) -> None:
    validate_initial_status(workflow, initial)
    with pytest.raises(InvalidTransition, match="initial status"):
        validate_initial_status(
            workflow,
            next(
                iter(
                    {
                        "Draft",
                        "Open",
                        "Pending",
                        "Approved",
                    }
                    - {initial}
                )
            ),
        )
