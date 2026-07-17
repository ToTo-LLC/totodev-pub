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
KEEP_LIST_NAME  = "_keep.txt"        # retention manifest at the CASE ROOT (case-relative rules)
LOGS_DIR_NAME   = "logs"             # per-case folder-logging tee (NOT under assets/)
LOG_FILE_NAME   = "case.log"         # the single appended per-case log file inside logs/

# Framework-owned keep rules seeded idempotently at case create/bind. Every rule is
# case-relative (exact path or glob). Purge deletes any file under the case folder
# that matches no keep rule. KEEP_LIST_NAME and LEASE_NAME are NOT listed here — both
# are unconditionally hard-skipped by CaseKeepManifest, so they never need to appear
# in their own rule list.
#
# logs/case.log is deliberately NOT seeded: the privacy default is that purge deletes
# the log like any other unmatched file. Callers who want it retained add an ordinary
# keep rule (e.g. case_keep_files("logs/case.log")), or set LogRetention.RETAIN so
# ensure_framework_rules() seeds CASE_LOG_KEEP_RULE at bind time.
FRAMEWORK_KEEP_RULES = (
    RECORD_NAME,
    f"{EVENTS_DIR_NAME}/**",
)

# Keep-rule path for the per-case log file. Seeded by ensure_framework_rules() when the
# process-global LogRetention is RETAIN; otherwise callers add it via case_keep_files().
CASE_LOG_KEEP_RULE = f"{LOGS_DIR_NAME}/{LOG_FILE_NAME}"

# Reserved case-owned artifacts at the case root; create_case_in_folder() rejects targets
# that already contain any of these names to avoid colliding with a prior case.
CASE_RESERVED_ARTIFACT_NAMES = (
    RECORD_NAME,
    EVENTS_DIR_NAME,
    ASSETS_DIR_NAME,
    KEEP_LIST_NAME,
    LEASE_NAME,
    LOGS_DIR_NAME,
)
# Event-log labels written by the FolderBackedCase base class. Every label is
# CASE_-prefixed so an observer can isolate the family's lifecycle events with a
# single CASE_* glob; subclasses are free to log their own labels alongside.
#
# CASE_BASE_EVENT_PREFIX is the class-family INVARIANT: every event label the base
# class auto-generates (now funneled through CaseEventJournal) MUST start with it, so a
# derived class can cleanly separate its own custom events from base lifecycle ones.
CASE_BASE_EVENT_PREFIX = "CASE_"

EV_STATE_ENTERED     = "CASE_STATE_ENTERED"     # current fine-grained state (value = state name)
EV_CREATED           = "CASE_CREATED"           # inception bookend
EV_TERMINATED        = "CASE_TERMINATED"        # terminal bookend (value = the terminal state entered)
EV_RECLASSIFIED      = "CASE_RECLASSIFIED"      # rebound to a different case subclass
EV_ALERTED           = "CASE_ALERTED"           # needs-a-human escalation marker
EV_TRANSITION_FAILED = "CASE_TRANSITION_FAILED" # pre-commit attempt failed (counted by @FAIL)
EV_ENTRY_EXCEPTION   = "CASE_ENTRY_EXCEPTION"   # post-commit on_enter/after raised (NOT counted)
EV_TRIGGER_SLOW      = "CASE_TRIGGER_SLOW"      # a trigger's work outran its soft timeout (warning)
EV_TRIGGER_TIMED_OUT = "CASE_TRIGGER_TIMED_OUT" # a trigger's work was hard-aborted at the kill ceiling
EV_TRIGGER_STARTED   = "CASE_TRIGGER_STARTED"   # a trigger's work slot began (value = trigger name);
                                            # resolved by the next STATE_ENTERED / TRANSITION_FAILED /
                                            # TRIGGER_TIMED_OUT / ENTRY_EXCEPTION — a dangling one
                                            # means in-flight (lease live) or crashed (lease gone)

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
