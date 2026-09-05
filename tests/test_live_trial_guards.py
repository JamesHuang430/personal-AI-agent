from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts.validate_creative_flow import check_trial, prepare_state


@pytest.mark.parametrize(
    "change",
    [
        {"target_seconds": 30},
        {"resolution": "2K"},
        {"planned_shots": 2},
        {"one_click": False},
        {"storyboard": [{}, {}]},
    ],
)
def test_live_trial_rejects_unapproved_spending(change):
    project = {
        "target_seconds": 4,
        "resolution": "768P",
        "planned_shots": 1,
        "one_click": True,
        "status": "awaiting_storyboard",
        "storyboard": [{}],
    }
    check_trial(project)
    with pytest.raises(ValueError, match="Stopped"):
        check_trial(project | change)


def test_unknown_creation_cannot_automatically_create_again(tmp_path):
    args = SimpleNamespace(
        state_file=str(tmp_path / "trial.json"),
        user_id=uuid4(),
        create=True,
        premise="A short video",
    )
    prepare_state(args)
    with pytest.raises(ValueError, match="Ambiguous prior creation"):
        prepare_state(args)


def test_live_trial_requires_explicit_creation(tmp_path):
    args = SimpleNamespace(state_file=str(tmp_path / "trial.json"), create=False)
    with pytest.raises(ValueError, match="--create explicitly"):
        prepare_state(args)
