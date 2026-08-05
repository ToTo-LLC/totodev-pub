# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Filesystem-layout constants for the CaseManager protocol."""

from __future__ import annotations

POLICY_SCHEMA_VERSION = 2
MANIFEST_PROTOCOL_VERSION = 1

# Liveness pulse: cadence of the manager's sibling pulse coroutine — the
# watchdog's kill-authorized signal. A blocked/starved event loop silences the
# pulse within one interval.
PULSE_INTERVAL_SECS = 0.5

DEFAULT_GROUPING_PATTERN = "{status}/"
DEFAULT_LIVE_BUCKET = "live"
DEFAULT_MANAGER_NAMESPACE = ".case_manager"

DEFAULT_STAGING_SUBDIR = "staging"
DEFAULT_ADOPT_DROP_SUBDIR = "adopt_drop"
DEFAULT_FIRE_MAILBOX_SUBDIR = "fire_mailbox"
DEFAULT_ADOPT_MAILBOX_SUBDIR = "adopt_mailbox"
DEFAULT_RECLASSIFY_MAILBOX_SUBDIR = "reclassify_mailbox"
DEFAULT_SHUTDOWN_MAILBOX_SUBDIR = "shutdown_mailbox"

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
