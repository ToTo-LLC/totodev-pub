# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Filesystem-layout constants for the CaseManager protocol."""

from __future__ import annotations

#: Bumped 2 → 3 by the request-queue cutover: the four per-action mailboxes became
#: one ``requests/`` queue and the two scratch docks became ``incoming/``. Layout
#: is immutable per filespace, so a filespace written at 2 cannot be opened at 3 —
#: there is no migration, by decision, because the layout was not yet depended on.
POLICY_SCHEMA_VERSION = 3
#: Bumped 1 → 2 alongside it: ``ManifestPaths`` no longer publishes per-mailbox
#: intakes, so a client compiled against 1 would look for paths that do not exist.
MANIFEST_PROTOCOL_VERSION = 2

# Liveness pulse: cadence of the manager's sibling pulse coroutine — the
# watchdog's kill-authorized signal. A blocked/starved event loop silences the
# pulse within one interval.
PULSE_INTERVAL_SECS = 0.5

DEFAULT_GROUPING_PATTERN = "{status}/"
DEFAULT_LIVE_BUCKET = "live"
DEFAULT_MANAGER_NAMESPACE = ".case_manager"

#: The one loading dock. Replaces ``staging/`` and ``adopt_drop/``, which did
#: nearly the same job under two cleaner rules with no stated rule for choosing
#: between them, and one of which was named after a reader rather than a content.
DEFAULT_INCOMING_SUBDIR = "incoming"
#: The one request channel. Replaces the four per-action mailboxes: the action
#: moved out of the folder path and into the message's ``op`` field.
DEFAULT_REQUESTS_SUBDIR = "requests"

# ---- Request queue stages ----
# One word per state, and each word means exactly one thing. The four mailboxes
# used three different words for "in progress" (``firing``, ``executing``, and
# adopt's ``pending``) while fire's ``pending`` meant "accepted, not started" —
# so ``pending`` denoted two opposite things in two sibling mailboxes.
QUEUED_STAGE = "queued"      # submitted; the manager has not claimed it yet
CLAIMED_STAGE = "claimed"    # claimed, awaiting a concurrency slot or choke permit
RUNNING_STAGE = "running"    # executing right now
FAILED_STAGE = "failed"      # gave up, or would not parse. A human must look
#: Shutdown keeps its own leaf rather than joining ``queued/``. It is process
#: control owned by the *host*, served whether or not an adapter exists, and its
#: protocol is "any non-hidden file" rather than a parsed envelope — routing it
#: through the adapter's queue would break ``enable_mailbox=False``.
SHUTDOWN_STAGE = "shutdown"

#: Stands in for a case id in the per-case stage folders. Reserved: a real case id
#: is a base-36 time slug or a uuid4 hex, so neither can begin with an underscore.
UNKNOWN_CASE_KEY = "_unknown"

POLICY_FILENAME = "case_manager_policy.yaml"
MANIFEST_FILENAME = "manifest.yaml"

# ---- Fleet status board (Fleet Status Board Spec) ----
FLEET_BOARD_PROTOCOL_VERSION = 1
FLEET_STATUS_FILENAME = "fleet_status.jsonl"

PLACEHOLDER_HEADER = (
    "# CaseManager cache placeholder — do not parse.\n"
    "# Authoritative case data lives in the slave directory (case_record.yaml).\n"
    "# This file format may change without notice.\n"
)

TERMINATION_SUBDIR = "termination"
EJECT_SUBDIR = "eject"
QUARANTINE_SUBDIR = "quarantine"
RESULTS_SUBDIR = "results"
