"""Standalone tests for ChokePermitGovernor — no pool drivers or FolderBackedCase."""

import asyncio

import pytest

from totodev_pub.folder_backed_case_support.choke_permit_governor import (
    ChokeGrant,
    ChokeGrantError,
    ChokePermitGovernor,
    InvalidChokeLimitsError,
)


def _gov(**limits: int) -> ChokePermitGovernor:
    return ChokePermitGovernor(dict(limits))


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_empty_limits_accepted():
    g = ChokePermitGovernor({})
    assert g.resource_usage() == {}


@pytest.mark.parametrize("bad", [0, -1, 1.5, "2"])
def test_invalid_limit_raises(bad):
    with pytest.raises(InvalidChokeLimitsError):
        ChokePermitGovernor({"cpu": bad})


# ---------------------------------------------------------------------------
# Sweep lifecycle
# ---------------------------------------------------------------------------

def test_try_acquire_outside_sweep_raises():
    g = _gov(cpu=1)
    with pytest.raises(RuntimeError, match="no sweep open"):
        g.try_acquire(frozenset({"cpu"}))


def test_nested_begin_sweep_raises():
    g = _gov(cpu=1)
    g.begin_sweep()
    with pytest.raises(RuntimeError, match="already open"):
        g.begin_sweep()
    g.end_sweep()


def test_end_sweep_idempotent():
    g = _gov(cpu=1)
    g.end_sweep()  # no-op


# ---------------------------------------------------------------------------
# Basic acquire / release
# ---------------------------------------------------------------------------

def test_empty_needed_returns_empty_grant():
    g = _gov(cpu=1)
    g.begin_sweep()
    grant = g.try_acquire(frozenset())
    assert isinstance(grant, ChokeGrant)
    g.release(grant)  # no-op
    g.end_sweep()


def test_single_resource_acquire_and_release():
    g = _gov(cpu=2)
    g.begin_sweep()
    grant = g.try_acquire(frozenset({"cpu"}))
    assert grant is not None
    assert g.resource_usage()["cpu"]["in_use"] == 1
    g.release(grant)
    assert g.resource_usage()["cpu"]["in_use"] == 0
    g.end_sweep()


def test_unknown_resource_raises_value_error():
    g = _gov(cpu=1)
    g.begin_sweep()
    with pytest.raises(ValueError, match="Unknown choke resource"):
        g.try_acquire(frozenset({"missing"}))
    g.end_sweep()


# ---------------------------------------------------------------------------
# All-or-nothing
# ---------------------------------------------------------------------------

def test_partial_availability_returns_none_and_holds_nothing():
    g = _gov(a=1, b=1)
    g.begin_sweep()
    first = g.try_acquire(frozenset({"a"}))
    assert first is not None
    second = g.try_acquire(frozenset({"a", "b"}))
    assert second is None
    assert g.resource_usage()["b"]["in_use"] == 0
    g.release(first)
    g.end_sweep()


def test_multi_resource_all_or_nothing_success():
    g = _gov(a=1, b=1)
    g.begin_sweep()
    grant = g.try_acquire(frozenset({"a", "b"}))
    assert grant is not None
    assert g.resource_usage()["a"]["in_use"] == 1
    assert g.resource_usage()["b"]["in_use"] == 1
    g.release(grant)
    g.end_sweep()


# ---------------------------------------------------------------------------
# Beat quantization
# ---------------------------------------------------------------------------

def test_mid_sweep_release_invisible_to_try_acquire():
    g = _gov(cpu=1)
    g.begin_sweep()
    grant = g.try_acquire(frozenset({"cpu"}))
    assert grant is not None
    g.release(grant)
    assert g.try_acquire(frozenset({"cpu"})) is None
    g.end_sweep()

    g.begin_sweep()
    assert g.try_acquire(frozenset({"cpu"})) is not None
    g.end_sweep()


# ---------------------------------------------------------------------------
# Overbooking prevention
# ---------------------------------------------------------------------------

def test_priority_grant_during_sweep_debits_sweep_budget():
    g = _gov(cpu=1)
    g.begin_sweep()
    sweep_grant = g.try_acquire(frozenset({"cpu"}))
    assert sweep_grant is not None
    assert g.try_acquire(frozenset({"cpu"})) is None
    g.release(sweep_grant)
    g.end_sweep()


@pytest.mark.asyncio
async def test_priority_mid_sweep_does_not_allow_try_acquire_overbook():
    g = _gov(cpu=1)
    g.begin_sweep()
    sweep_grant = g.try_acquire(frozenset({"cpu"}))
    assert sweep_grant is not None

    got_priority = asyncio.create_task(g.acquire_priority(frozenset({"cpu"})))
    await asyncio.sleep(0)
    assert g.priority_waiter_count() == 1
    assert g.try_acquire(frozenset({"cpu"})) is None

    g.release(sweep_grant)
    priority_grant = await got_priority
    assert priority_grant is not None
    assert g.try_acquire(frozenset({"cpu"})) is None

    g.release(priority_grant)
    g.end_sweep()


# ---------------------------------------------------------------------------
# Priority acquire (async)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_priority_immediate_when_capacity_available():
    g = _gov(cpu=1)
    grant = await g.acquire_priority(frozenset({"cpu"}))
    assert grant is not None
    assert g.resource_usage()["cpu"]["in_use"] == 1
    g.release(grant)


@pytest.mark.asyncio
async def test_priority_waits_and_wakes_on_release():
    g = _gov(cpu=1)
    g.begin_sweep()
    hold = g.try_acquire(frozenset({"cpu"}))
    assert hold is not None

    waiter = asyncio.create_task(g.acquire_priority(frozenset({"cpu"})))
    await asyncio.sleep(0)
    assert g.priority_waiter_count() == 1
    assert not waiter.done()

    g.release(hold)
    grant = await waiter
    assert grant is not None
    g.release(grant)
    g.end_sweep()


@pytest.mark.asyncio
async def test_priority_fifo_two_waiters():
    g = _gov(cpu=1)
    hold = (await g.acquire_priority(frozenset({"cpu"})))

    order: list[str] = []

    async def wait_and_record(tag: str) -> None:
        grant = await g.acquire_priority(frozenset({"cpu"}))
        order.append(tag)
        g.release(grant)

    t1 = asyncio.create_task(wait_and_record("first"))
    t2 = asyncio.create_task(wait_and_record("second"))
    await asyncio.sleep(0)
    assert g.priority_waiter_count() == 2

    g.release(hold)
    await t1
    await t2
    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_overlapping_priority_waiters_whole_set_only():
    g = _gov(a=1, b=1)
    hold_a = await g.acquire_priority(frozenset({"a"}))
    hold_b = await g.acquire_priority(frozenset({"b"}))

    waiter = asyncio.create_task(g.acquire_priority(frozenset({"a", "b"})))
    await asyncio.sleep(0)
    assert g.priority_waiter_count() == 1
    assert not waiter.done()

    g.release(hold_a)
    await asyncio.sleep(0)
    assert not waiter.done()

    g.release(hold_b)
    grant = await waiter
    assert grant is not None
    g.release(grant)


# ---------------------------------------------------------------------------
# Grant safety
# ---------------------------------------------------------------------------

def test_double_release_raises():
    g = _gov(cpu=1)
    g.begin_sweep()
    grant = g.try_acquire(frozenset({"cpu"}))
    assert grant is not None
    g.release(grant)
    with pytest.raises(ChokeGrantError):
        g.release(grant)
    g.end_sweep()


def test_foreign_grant_raises():
    g = _gov(cpu=1)
    other = _gov(cpu=1)
    other.begin_sweep()
    other_grant = other.try_acquire(frozenset({"cpu"}))
    assert other_grant is not None
    with pytest.raises(ChokeGrantError):
        g.release(other_grant)
    other.release(other_grant)
    other.end_sweep()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def test_resource_usage_and_waiter_count():
    g = _gov(cpu=2, api=1)
    assert g.resource_usage() == {
        "cpu": {"limit": 2, "in_use": 0},
        "api": {"limit": 1, "in_use": 0},
    }
    assert g.priority_waiter_count() == 0


@pytest.mark.asyncio
async def test_priority_unknown_resource_raises():
    g = _gov(cpu=1)
    with pytest.raises(ValueError, match="Unknown choke resource"):
        await g.acquire_priority(frozenset({"nope"}))
