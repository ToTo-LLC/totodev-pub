# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from pathlib import Path

import pytest
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.primitive_event_log import PrimitiveEventLog


class _TypedPayload(BaseModel, FileMappedPydanticMixin):
    """Minimal Pydantic payload used to verify typed event serialization."""

    message: str
    count: int

    def __init__(self, **data):
        super().__init__(**data)
        self._persisted_file_path = None
        self._absolute_file_path = None
        self._lock_acquired = False
        self._has_unsaved_changes = False
        self._original_state = None
        self._file = None
        self._file_stat = None
        self._last_loaded_at = None
        self._on_file_modified_callback = None
        self._in_context_manager = False
        self._format_override = None


@pytest.fixture
def event_dir(tmp_path: Path) -> Path:
    target_dir = tmp_path / "volatile" / "primitive_event_log"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def test_create_event_sequences_and_payloads(event_dir: Path) -> None:
    log = PrimitiveEventLog(event_dir)

    empty_event = log.create_event("OCR-STATUS", "QUEUED")
    dict_event = log.create_event("OCR-STATUS", "COMPLETED", {"pages": 5, "confidence": 0.98})

    proxies = list(log.events(recent_first=False))

    assert [proxy.label_value for proxy in proxies] == [
        "OCR-STATUS@QUEUED",
        "OCR-STATUS@COMPLETED",
    ]
    assert proxies[0].file_path.name.startswith("e001_")
    assert proxies[1].file_path.name.startswith("e002_")

    assert empty_event.contents() is None

    payload = dict_event.contents()
    assert payload is not None
    assert payload.as_dict() == {"pages": 5, "confidence": 0.98}


def test_latest_values_and_has_event(event_dir: Path) -> None:
    log = PrimitiveEventLog(event_dir)

    log.create_event("OCR-STATUS", "QUEUED")
    log.create_event("OCR-STATUS", "DONE")
    log.create_event("VALIDATION-STATUS", "PENDING")

    latest = log.latest_values()

    assert latest["OCR-STATUS"] == "DONE"
    assert latest["VALIDATION-STATUS"] == "PENDING"

    assert log.has_event("OCR-STATUS") == "DONE"
    assert log.has_event("MISSING-LABEL") is False


def test_typed_payload_round_trip(event_dir: Path) -> None:
    log = PrimitiveEventLog(event_dir)

    payload = _TypedPayload(message="ready", count=3)

    event = log.create_event("PIPELINE-STATUS", "READY", payload)

    typed = event.contents(load_class=_TypedPayload)

    assert isinstance(typed, _TypedPayload)
    assert typed.message == "ready"
    assert typed.count == 3


def test_segment_events_partitions_on_marker(event_dir: Path) -> None:
    log = PrimitiveEventLog(event_dir)

    log.create_event("NOISE", "IGNORED")
    log.create_event("STATE", "ENTER")
    log.create_event("ACTION", "RUNNING")
    log.create_event("STATE", "ENTER")
    log.create_event("ACTION", "COOLDOWN")

    segments = list(log.segment_events("STATE", start_value_glob="ENTER"))

    assert len(segments) == 2
    assert [event.label_value for event in segments[0]] == ["STATE@ENTER", "ACTION@RUNNING"]
    assert [event.label_value for event in segments[1]] == ["STATE@ENTER", "ACTION@COOLDOWN"]


def test_segment_events_accepts_multiple_patterns(event_dir: Path) -> None:
    log = PrimitiveEventLog(event_dir)

    log.create_event("STATE", "ENTER")
    log.create_event("ACTION", "RUNNING")
    log.create_event("RESET", "TRIGGERED")
    log.create_event("ACTION", "AFTER-RESET")

    segments = list(log.segment_events(["STATE", "RESET"]))

    assert len(segments) == 2
    assert [event.label_value for event in segments[0]] == ["STATE@ENTER", "ACTION@RUNNING"]
    assert [event.label_value for event in segments[1]] == ["RESET@TRIGGERED", "ACTION@AFTER-RESET"]


def test_segment_events_returns_empty_when_no_markers(event_dir: Path) -> None:
    log = PrimitiveEventLog(event_dir)

    log.create_event("ACTION", "ONE")
    log.create_event("ACTION", "TWO")

    segments = list(log.segment_events("STATE"))

    assert segments == []

