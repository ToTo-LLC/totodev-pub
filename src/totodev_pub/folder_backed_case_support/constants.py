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

# In-flight lease keepalive: while a trigger's awaited work runs, a sibling "pulse" task
# beats the lease every lease_ttl_for(state) / LEASE_PULSE_FRACTION_DIVISOR seconds, so a
# legitimately long step does not let the lease lapse out from under a live owner. The
# default of 3 gives two beats before expiry (a missed beat still leaves a margin).
LEASE_PULSE_FRACTION_DIVISOR = 3.0
