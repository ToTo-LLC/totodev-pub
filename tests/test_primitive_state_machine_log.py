# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from totodev_pub.cached_file_folders_support.primitive_state_machine_log import (
    DEFAULT_ENTER_LABEL,
    DEFAULT_ERROR_LABEL,
    PrimitiveStateMachineLog,
)
from totodev_pub.primitive_event_log import PrimitiveEventLog


def _make_state_machine(
    tmp_path: Path,
    subdir: str = "events",
    *,
    entry_label: str = DEFAULT_ENTER_LABEL,
    error_label: str = DEFAULT_ERROR_LABEL,
    init_state: Optional[str] = None,
) -> Tuple[PrimitiveStateMachineLog, PrimitiveEventLog]:
    event_dir = tmp_path / "volatile" / subdir
    event_dir.mkdir(parents=True, exist_ok=True)
    event_log = PrimitiveEventLog(event_dir)
    state_machine = PrimitiveStateMachineLog(
        event_log,
        init_state=init_state,
        entry_label=entry_label,
        error_label=error_label,
    )
    return state_machine, event_log


def test_init_state_creates_initial_entry_when_missing(tmp_path: Path) -> None:
    state_machine, _ = _make_state_machine(tmp_path, init_state="READY")

    current = state_machine.cur_state()

    assert current is not None
    assert current.label == DEFAULT_ENTER_LABEL
    assert current.value == "READY"


def test_init_state_does_not_overwrite_existing_state(tmp_path: Path) -> None:
    state_machine, event_log = _make_state_machine(tmp_path)
    event_log.create_event(DEFAULT_ENTER_LABEL, "EXISTING")

    state_machine_with_init = PrimitiveStateMachineLog(
        event_log, init_state="IGNORED"
    )

    current = state_machine_with_init.cur_state()

    assert current is not None
    assert current.value == "EXISTING"


def test_state_progression_and_prior_state(tmp_path: Path) -> None:
    state_machine, _ = _make_state_machine(tmp_path)

    assert state_machine.cur_state() is None
    assert state_machine.prior_state() is None

    state_machine.log_enter("DRAFT")
    first_state = state_machine.cur_state()

    assert first_state is not None
    assert first_state.value == "DRAFT"
    assert state_machine.prior_state() is None

    state_machine.log_enter("REVIEW")
    current_state = state_machine.cur_state()
    prior_state = state_machine.prior_state()

    assert current_state is not None
    assert prior_state is not None
    assert current_state.value == "REVIEW"
    assert prior_state.value == "DRAFT"


def test_cur_state_errors_filters_previous_states(tmp_path: Path) -> None:
    state_machine, _ = _make_state_machine(tmp_path)

    state_machine.log_enter("DRAFT")
    state_machine.log_error({"code": 1})
    state_machine.log_error({"code": 2})

    draft_errors = state_machine.cur_state_errors()
    assert [error.value for error in draft_errors] == ["DRAFT", "DRAFT"]

    state_machine.log_enter("REVIEW")
    assert state_machine.cur_state_errors() == []

    state_machine.log_error({"code": 3})
    review_errors = state_machine.cur_state_errors()

    assert len(review_errors) == 1
    assert review_errors[0].label == DEFAULT_ERROR_LABEL
    assert review_errors[0].value == "REVIEW"


def test_custom_labels_are_respected(tmp_path: Path) -> None:
    custom_entry = "GO_STATE"
    custom_error = "UH_OH_STATE"
    state_machine, _ = _make_state_machine(
        tmp_path,
        subdir="custom",
        entry_label=custom_entry,
        error_label=custom_error,
    )

    state_machine.log_enter("INIT")
    state_machine.log_error()

    current = state_machine.cur_state()
    errors = state_machine.cur_state_errors()

    assert current is not None
    assert current.label == custom_entry
    assert errors and errors[0].label == custom_error


def test_label_properties_reflect_configuration(tmp_path: Path) -> None:
    custom_entry = "HELLO"
    custom_error = "WHOOPS"
    state_machine, _ = _make_state_machine(
        tmp_path, entry_label=custom_entry, error_label=custom_error
    )

    assert state_machine.entry_label == custom_entry
    assert state_machine.error_label == custom_error


def test_transition_history_partitions_events(tmp_path: Path) -> None:
    state_machine, _ = _make_state_machine(tmp_path)

    state_machine.log_enter("A")
    state_machine.log_error({"msg": "oops"})
    state_machine.log_enter("B")
    state_machine.log_enter("C")
    state_machine.log_error({"msg": "again"})

    history = state_machine.transition_history(depth=0)
    assert len(history) == 3
    assert [segment[0].value for segment in history] == ["A", "B", "C"]
    assert [event.value for event in history[0][1:]] == ["A"]

    latest = state_machine.transition_history(depth=1)
    assert len(latest) == 1
    assert latest[0][0].value == "C"
    assert [e.value for e in latest[0][1:]] == ["C"]

