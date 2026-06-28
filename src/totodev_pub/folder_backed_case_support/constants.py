# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Filesystem-layout constants for the FolderBackedCase family.

Centralized here so every support module agrees on the on-disk names of a case
folder's pieces (record, lease, asset playground, retention manifest)."""

from __future__ import annotations

RECORD_NAME     = "case_record.yaml"
LEASE_NAME      = ".case.lease"      # single-owner lease: content-free; mtime = "valid-until"
EVENTS_DIR_NAME = "events"           # case event-log folder (PrimitiveEventLog storage)
ASSETS_DIR_NAME = "assets"           # the downstream-owned asset "playground"
KEEP_LIST_NAME  = "_keep_assets.txt" # retention manifest at the CASE ROOT (NOT under assets/)

# Reserved case-owned artifacts at the case root; create_case_in_folder() rejects targets
# that already contain any of these names to avoid colliding with a prior case.
CASE_RESERVED_ARTIFACT_NAMES = (
    RECORD_NAME,
    EVENTS_DIR_NAME,
    ASSETS_DIR_NAME,
    KEEP_LIST_NAME,
    LEASE_NAME,
)

# Event-log labels written by the FolderBackedCase base class. Every label is
# CASE_-prefixed so an observer can isolate the family's lifecycle events with a
# single CASE_* glob; subclasses are free to log their own labels alongside.
#
# CASE_BASE_EVENT_PREFIX is the class-family INVARIANT: every event label the base
# class auto-generates (now funneled through CaseJournal) MUST start with it, so a
# derived class can cleanly separate its own custom events from base lifecycle ones.
# SIG_CLOSING reuses the prefix but is an in-memory listener signal, never logged.
CASE_BASE_EVENT_PREFIX = "CASE_"

EV_ENTER_STATE     = "CASE_ENTER_STATE"      # current fine-grained state (value = state name)
EV_NEW             = "CASE_NEW"              # inception bookend
EV_CLOSED          = "CASE_CLOSED"          # terminal bookend (value = closing state)
EV_RECLASSIFY      = "CASE_RECLASSIFY"      # rebound to a different case subclass
EV_ALERT           = "CASE_ALERT"           # needs-a-human escalation marker
EV_FAIL_TRANSITION = "CASE_FAIL_TRANSITION" # pre-commit attempt failed (counted by @FAIL)
EV_ENTRY_EXCEPTION = "CASE_ENTRY_EXCEPTION" # post-commit on_enter/after raised (NOT counted)
EV_TRIGGER_SLOW    = "CASE_TRIGGER_SLOW"    # a trigger's work outran its soft timeout (warning)
EV_TRIGGER_TIMEOUT = "CASE_TRIGGER_TIMEOUT" # a trigger's work was hard-aborted at the kill ceiling

# In-memory listener signal (passed to add_transition_listener callbacks, not logged).
# The closed signal reuses EV_CLOSED; only the phase-1 closing signal is distinct.
SIG_CLOSING = "CASE_CLOSING"   # phase-1 close: assets still present

# Trigger timeout policy shared by FolderBackedCase and _CaseMachineFactory.
DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS = 5.0
TIMEOUT_KILL_MULTIPLE_OF_WARNING = 2

# ---- Heartbeat-lease timing (fixed, non-overridable by design) ----
# These three numbers fully describe the lease's timing. They are deliberately NOT exposed as
# per-case/per-state override seams (YAGNI): one fixed policy is far easier to reason about,
# and the in-flight pulse already decouples a long single step from the TTL. A later release
# may add an override mechanism if a real need appears; until then, change them HERE.
#
# DEFAULT_LEASE_TTL_SECS is purely a CRASH-RECOVERY WINDOW: how long another owner waits,
# after this one vanishes (crash / freeze / kill), before treating the folder as abandoned and
# reclaiming it. It is NOT a knob for "how long a state idles" — a live owner stays alive
# indefinitely by beating, and keeping an idle case alive is the holder's / CaseManager's job,
# never a longer TTL. Kept short so a dead owner is reclaimed promptly; a live owner renews it
# cheaply (one stat + touch) far inside the window, so a short TTL costs almost nothing.
DEFAULT_LEASE_TTL_SECS = 30.0

# Opportunistic beats (case_advance() pre-step + each transition boundary) are throttled to at
# most one actual file write this often, so a tight drive loop does not hammer the disk. Must
# stay well under the TTL so a live-but-quiet owner always re-stamps before expiry.
LEASE_HEARTBEAT_THROTTLE_SECS = 10.0

# In-flight keepalive cadence: while a trigger's awaited work runs, a sibling "pulse" task
# beats the lease every DEFAULT_LEASE_TTL_SECS / LEASE_PULSE_FRACTION_DIVISOR seconds, so a
# legitimately long step never lets the lease lapse out from under a live owner. A divisor of
# 3 gives two beats before expiry (a single missed beat still leaves a margin).
LEASE_PULSE_FRACTION_DIVISOR = 3.0

# Fail-fast invariants — checked once at import because the values are fixed. A live owner must
# get at least two writes in per TTL via BOTH the opportunistic-beat throttle and the pulse.
assert 0.0 < LEASE_HEARTBEAT_THROTTLE_SECS <= DEFAULT_LEASE_TTL_SECS / 2.0
assert LEASE_PULSE_FRACTION_DIVISOR >= 2.0
