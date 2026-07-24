# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import time
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


# ---- cache_msecs ----
#
# These write a raw event file directly to disk (bypassing create_event(),
# which invalidates the cache) to simulate a change landing on the folder
# while a cache_msecs > 0 caller's cached scan is still considered fresh.


def _write_raw_event(event_dir: Path, filename: str) -> None:
    (event_dir / filename).touch()


class _FakeMonotonic:
    """Controllable stand-in for time.monotonic(), advanced explicitly."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeMonotonic:
    clock = _FakeMonotonic()
    monkeypatch.setattr(time, "monotonic", clock)
    return clock


def test_events_without_cache_msecs_always_rescans(event_dir: Path, fake_clock: _FakeMonotonic) -> None:
    log = PrimitiveEventLog(event_dir)
    log.create_event("STATUS", "ONE")

    assert len(list(log.events())) == 1

    _write_raw_event(event_dir, "e002_STATUS@TWO.yaml")

    # Default cache_msecs=0: the new file is visible immediately, no lag.
    assert len(list(log.events())) == 2
    assert len(list(log.events(cache_msecs=0))) == 2


def test_events_cache_msecs_serves_stale_scan_then_refreshes(
    event_dir: Path, fake_clock: _FakeMonotonic
) -> None:
    log = PrimitiveEventLog(event_dir)
    log.create_event("STATUS", "ONE")

    first = list(log.events(cache_msecs=1000))
    assert len(first) == 1

    _write_raw_event(event_dir, "e002_STATUS@TWO.yaml")

    # Still within the 1000ms window: the cached (stale) scan is served.
    fake_clock.advance(0.05)
    stale = list(log.events(cache_msecs=1000))
    assert len(stale) == 1

    # Past the window: a fresh scan picks up the new file.
    fake_clock.advance(2.0)
    fresh = list(log.events(cache_msecs=1000))
    assert len(fresh) == 2


def test_cache_scan_is_shared_across_different_glob_filters(
    event_dir: Path, fake_clock: _FakeMonotonic
) -> None:
    log = PrimitiveEventLog(event_dir)
    log.create_event("OCR-STATUS", "QUEUED")

    # Populate the cache via one label_glob...
    assert len(list(log.events(label_glob="OCR-*", cache_msecs=1000))) == 1

    _write_raw_event(event_dir, "e002_VALIDATION-STATUS@PENDING.yaml")

    # ...a different label_glob within the window still sees the stale
    # (pre-write) snapshot: one shared cache, not one per glob combination.
    fake_clock.advance(0.05)
    assert list(log.events(label_glob="VALIDATION-*", cache_msecs=1000)) == []


def test_create_event_invalidates_cache(event_dir: Path, fake_clock: _FakeMonotonic) -> None:
    log = PrimitiveEventLog(event_dir)
    log.create_event("STATUS", "ONE")

    assert len(list(log.events(cache_msecs=60_000))) == 1

    # No time advance at all: a naive cache would still call this "fresh".
    log.create_event("STATUS", "TWO")

    assert len(list(log.events(cache_msecs=60_000))) == 2


def test_purge_invalidates_cache(event_dir: Path, fake_clock: _FakeMonotonic) -> None:
    log = PrimitiveEventLog(event_dir)
    log.create_event("STATUS", "ONE")
    list(log.events(cache_msecs=60_000))  # populate the cache

    log.purge()
    log.create_event("STATUS", "FRESH")

    remaining = list(log.events(cache_msecs=60_000))
    assert [proxy.label_value for proxy in remaining] == ["STATUS@FRESH"]


def test_has_event_respects_cache_msecs(event_dir: Path, fake_clock: _FakeMonotonic) -> None:
    log = PrimitiveEventLog(event_dir)
    log.create_event("OCR-STATUS", "QUEUED")

    assert log.has_event("OCR-STATUS", cache_msecs=1000) == "QUEUED"

    _write_raw_event(event_dir, "e002_OCR-STATUS@DONE.yaml")

    fake_clock.advance(0.05)
    assert log.has_event("OCR-STATUS", cache_msecs=1000) == "QUEUED"  # stale, cached

    fake_clock.advance(2.0)
    assert log.has_event("OCR-STATUS", cache_msecs=1000) == "DONE"  # refreshed


def test_latest_values_respects_cache_msecs(event_dir: Path, fake_clock: _FakeMonotonic) -> None:
    log = PrimitiveEventLog(event_dir)
    log.create_event("OCR-STATUS", "QUEUED")

    assert log.latest_values(cache_msecs=1000)["OCR-STATUS"] == "QUEUED"

    _write_raw_event(event_dir, "e002_OCR-STATUS@DONE.yaml")

    fake_clock.advance(0.05)
    assert log.latest_values(cache_msecs=1000)["OCR-STATUS"] == "QUEUED"  # stale, cached

    fake_clock.advance(2.0)
    assert log.latest_values(cache_msecs=1000)["OCR-STATUS"] == "DONE"  # refreshed


def test_segment_events_respects_cache_msecs(event_dir: Path, fake_clock: _FakeMonotonic) -> None:
    log = PrimitiveEventLog(event_dir)
    log.create_event("STATE", "ENTER")
    log.create_event("ACTION", "RUNNING")

    first = list(log.segment_events("STATE", cache_msecs=1000))
    assert len(first) == 1
    assert [event.label_value for event in first[0]] == ["STATE@ENTER", "ACTION@RUNNING"]

    _write_raw_event(event_dir, "e003_STATE@ENTER.yaml")
    _write_raw_event(event_dir, "e004_ACTION@COOLDOWN.yaml")

    fake_clock.advance(0.05)
    stale = list(log.segment_events("STATE", cache_msecs=1000))
    assert len(stale) == 1  # cached scan predates the new segment

    fake_clock.advance(2.0)
    fresh = list(log.segment_events("STATE", cache_msecs=1000))
    assert len(fresh) == 2

