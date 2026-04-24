# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

# test_self_throttle.py

from pytest import fixture
from tempfile import NamedTemporaryFile
import os
import pytest
import time
import tempfile
import asyncio
from typing import Optional
from contextlib import contextmanager
from totodev_pub.pytest_tools import very_lazy_test
from totodev_pub.llm.self_throttle import (
    _CharUsageLogBase,
    _InMemoryCharUsageLog,
    _FilePersistedCharUsageLog,
    SelfBandwidthThrottle
)

pytest.importorskip("pytest_asyncio")

FORCE_LAZY_TESTS_TO_RUN_PERIOD = 20 # randomly ensure tests are run roughly with this period


def simple_usage_scenario_test(usage_log_obj: _CharUsageLogBase):
    log:_CharUsageLogBase = usage_log_obj
    assert log.default_cutoff_secs == 60
    cur_t = log.cur_timestamp()
    entries = [5,4,3,2,1]
    # Add an irrelevant old entry that should be ignored
    log.log(9999999, cur_t - log.default_cutoff_secs*2)
    for i in entries:
        log.log(i, cur_t - i)
    # Retrieve what was just logged
    log_results = log.charcounts_and_timestamps(cur_t - entries[0] - 0.001)
    assert log_results == [(i, cur_t - i) for i in entries]

    # Add a new entry at the current time
    newest_val = 555
    log.log(newest_val)
    assert log.charcounts_and_timestamps(cur_t - entries[0] - .001)[-1][0] == newest_val


def test_in_memory_log():
    simple_usage_scenario_test(_InMemoryCharUsageLog())


def test_file_persisted_log(tmp_path):
    with NamedTemporaryFile(suffix=".sqlite") as tmp_file:
        tfname = tmp_file.name
        tmp_file.close()
        if os.path.exists(tfname):
            os.unlink(tfname)
        simple_usage_scenario_test(_FilePersistedCharUsageLog(tmp_file.name))


@contextmanager
def maybe_tempfile(cross_process: bool):
    """Context manager that yields either None or a named temp file."""
    if cross_process:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as f:
            f.close()
            if os.path.exists(f.name):
                os.unlink(f.name)
            yield f.name
    else:
        yield None


@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
@pytest.mark.parametrize("cross_process", [False, True])
def test_under_limit_no_wait(cross_process):
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=60, cross_process_logfile=fname)
        wait = throttle.suggest_throttle_seconds(request_chars=10)
        assert wait == 0
        # No call to sleep_on_throttle; we just verify no waiting is required.


@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
@pytest.mark.parametrize("cross_process", [False, True])
def test_just_reaches_limit(cross_process):
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=60, cross_process_logfile=fname)
        throttle.suggest_throttle_seconds(90)  # reserve 90 chars
        wait = throttle.suggest_throttle_seconds(10)  # exactly at limit
        assert wait == 0
        # No waiting required.


@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
@pytest.mark.parametrize("cross_process", [False, True])
def test_exceeds_limit(cross_process):
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=60, cross_process_logfile=fname)
        throttle.suggest_throttle_seconds(100)  # fully use limit
        wait = throttle.suggest_throttle_seconds(10)  # now exceed limit
        assert wait > 0
        # Without sleep_on_throttle, we just confirm that a wait is needed.


@pytest.mark.parametrize("cross_process", [False, True])
def test_no_entries_yet(cross_process):
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=60, cross_process_logfile=fname)
        assert throttle.sent_window_char_count() == 0


@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
@pytest.mark.parametrize("cross_process", [False, True])
def test_sent_count_updates(cross_process):
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=60, cross_process_logfile=fname)
        throttle.suggest_throttle_seconds(30)
        throttle.suggest_throttle_seconds(20)
        assert throttle.sent_window_char_count() == 50


@very_lazy_test(['totodev_pub.llm.self_throttle'], random_period=20)
@pytest.mark.parametrize("cross_process", [False, True])
def test_expiration_of_old_entries(cross_process: bool):
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=1, cross_process_logfile=fname)
        throttle.suggest_throttle_seconds(60)
        time.sleep(2)  # entries older than 1 second should expire
        assert throttle.sent_window_char_count() == 0


@pytest.mark.asyncio
@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
@pytest.mark.parametrize("cross_process", [False, True])
async def test_future_reservation_behavior(cross_process):
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(char_limit=50, interval_secs=5, cross_process_logfile=fname)
        throttle.suggest_throttle_seconds(50)  # at limit
        wait = throttle.suggest_throttle_seconds(10)  # exceeds limit
        assert wait > 0
        # Simulate waiting the required time
        await asyncio.sleep(wait + 0.1)
        # After waiting, the count should drop
        assert throttle.sent_window_char_count() <= 10


@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
@pytest.mark.parametrize("cross_process", [False, True])
def test_multiple_calls_stay_under_limit(cross_process):
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=60, cross_process_logfile=fname)
        for _ in range(5):
            wait = throttle.suggest_throttle_seconds(20)
            assert wait == 0
        assert throttle.sent_window_char_count() == 100


@pytest.mark.asyncio
@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
@pytest.mark.parametrize("cross_process", [False, True])
async def test_attempting_large_request(cross_process):
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=2, cross_process_logfile=fname)
        # Use entire budget
        throttle.suggest_throttle_seconds(100)
        # Large request
        wait = throttle.suggest_throttle_seconds(200)
        assert wait > 0

        # Simulate waiting the required time ourselves
        await asyncio.sleep(wait + 0.1)
        # After waiting, a small request should require no wait
        new_wait = throttle.suggest_throttle_seconds(10)
        assert new_wait < 2   # should be little or no wait time


@pytest.mark.slow
@very_lazy_test(['totodev_pub.llm.self_throttle'], reverify_days=14)
@pytest.mark.parametrize("cross_process", [False, True])
def test_request_exactly_after_interval(cross_process):
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=1, cross_process_logfile=fname)
        throttle.suggest_throttle_seconds(100)  # full
        time.sleep(1)  # interval passes
        assert throttle.sent_window_char_count() == 0
        wait = throttle.suggest_throttle_seconds(10)
        assert wait == 0


@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
def test_underloaded_throttle():
    throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=60)
    wait = throttle.suggest_throttle_seconds(0)
    assert wait == 0
    assert throttle.sent_window_char_count() == 0

    assert throttle.suggest_throttle_seconds(10) == 0
    assert throttle.sent_window_char_count() == 10

    assert throttle.suggest_throttle_seconds(20) == 0
    assert throttle.sent_window_char_count() == 30

    assert throttle.suggest_throttle_seconds(70) == 0
    assert throttle.sent_window_char_count() == 100

    # This should exceed the limit and trigger a wait
    assert throttle.suggest_throttle_seconds(20) > 0    


@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
def test_heavy_overload_throttle():
    throttle = SelfBandwidthThrottle(char_limit=100, interval_secs=60)
    assert throttle.suggest_throttle_seconds(throttle.char_limit/2+10) == 0

    # When the requests start stacking up, the throttle should push suggest later and later times
    assert throttle.suggest_throttle_seconds(throttle.char_limit/2) >= (throttle.interval-1)
    assert throttle.suggest_throttle_seconds(throttle.char_limit+1) >= 2*(throttle.interval-1)
    assert throttle.suggest_throttle_seconds(throttle.char_limit+1) >= 3*(throttle.interval-1)


@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
@pytest.mark.parametrize("cross_process", [False, True])
def test_max_requests_limit(cross_process):
    """Test that max_requests limit is enforced regardless of char count"""
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(
            char_limit=1000,  # High char limit
            interval_secs=60,
            cross_process_logfile=fname,
            max_requests=3  # Only allow 3 requests per interval
        )
        
        # First 3 requests should be allowed immediately (even with 0 chars)
        assert throttle.suggest_throttle_seconds(1) == 0
        assert throttle.suggest_throttle_seconds(1) == 0
        assert throttle.suggest_throttle_seconds(1) == 0
        
        # Fourth request should be throttled even with minimal chars
        wait_time = throttle.suggest_throttle_seconds(1)
        assert wait_time > 0, "Should be throttled after max_requests"


@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
@pytest.mark.parametrize("cross_process", [False, True])
def test_max_requests_reset(cross_process, monkeypatch):
    """Test that max_requests counter resets after interval"""
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(
            char_limit=1000,
            interval_secs=60,
            cross_process_logfile=fname,
            max_requests=2
        )
        
        # Use up the request quota
        assert throttle.suggest_throttle_seconds(1) == 0
        assert throttle.suggest_throttle_seconds(1) == 0
        
        # Should be throttled
        assert throttle.suggest_throttle_seconds(1) > 0
        
        # Mock time to advance past interval
        current_time = time.time()
        def mock_time():
            return current_time + 61  # Just past the 60 second interval
        
        monkeypatch.setattr(time, 'time', mock_time)
        
        # Also mock the cur_timestamp method for file-persisted logs
        if cross_process:
            monkeypatch.setattr(throttle._token_log, 'cur_timestamp', mock_time)
        
        # Should be allowed again after interval
        assert throttle.suggest_throttle_seconds(1) == 0


@very_lazy_test(["totodev_pub.llm.self_throttle"], random_period=FORCE_LAZY_TESTS_TO_RUN_PERIOD)
@pytest.mark.parametrize("cross_process", [False, True])
def test_max_requests_and_char_limit(cross_process):
    """Test that both max_requests and char_limit are enforced"""
    with maybe_tempfile(cross_process) as fname:
        throttle = SelfBandwidthThrottle(
            char_limit=10,  # Low char limit
            interval_secs=60,
            cross_process_logfile=fname,
            max_requests=5  # Higher request limit
        )
        
        # Should be throttled by char_limit before hitting request limit
        assert throttle.suggest_throttle_seconds(8) == 0
        
        # Should be throttled by char count even though requests < max
        wait_time = throttle.suggest_throttle_seconds(8)
        assert wait_time > 0, "Should be throttled by char_limit before max_requests"
