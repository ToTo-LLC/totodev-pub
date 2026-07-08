# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Filesystem-layout constants for the CaseManager protocol."""

from __future__ import annotations

POLICY_SCHEMA_VERSION = 1
MANIFEST_PROTOCOL_VERSION = 1

DEFAULT_GROUPING_PATTERN = "{bucket}/"
DEFAULT_LIVE_BUCKET = "live"
DEFAULT_TERMINAL_PREFIX = "terminal"
DEFAULT_ABERRANT_BUCKET = "aberrant"
DEFAULT_MANAGER_NAMESPACE = ".case_manager"
DEFAULT_CASE_REF_PATH_TEMPLATE = "cases/{case_id}.yaml"

DEFAULT_STAGING_SUBDIR = "staging"
DEFAULT_ADOPT_DROP_SUBDIR = "adopt_drop"
DEFAULT_FIRE_MAILBOX_SUBDIR = "fire_mailbox"
DEFAULT_ADOPT_MAILBOX_SUBDIR = "adopt_mailbox"
DEFAULT_RECLASSIFY_MAILBOX_SUBDIR = "reclassify_mailbox"

POLICY_FILENAME = "case_manager_policy.yaml"
MANIFEST_FILENAME = "manifest.yaml"

# ---- Fleet status board (Fleet Status Board Spec) ----
FLEET_BOARD_PROTOCOL_VERSION = 1
FLEET_STATUS_FILENAME = "fleet_status.jsonl"
# The board file always exists at its known location; when the feature is off it
# holds exactly this sentinel comment. Readers detect it by the stable prefix.
FLEET_BOARD_DISABLED_PREFIX = "# Fleet status board disabled"
FLEET_BOARD_DISABLED_SENTINEL = (
    f"{FLEET_BOARD_DISABLED_PREFIX} — set `enable_fleet_status_board=False` in "
    "your CaseManagerPolicy to keep the board off (it is on by default)."
)

PLACEHOLDER_HEADER = (
    "# CaseManager cache placeholder — do not parse.\n"
    "# Authoritative case data lives in the slave directory (case_record.yaml).\n"
    "# This file format may change without notice.\n"
)

TERMINATION_SUBDIR = "termination"
EJECT_SUBDIR = "eject"
RESULTS_SUBDIR = "results"
ABERRANT_META_SUBDIR = "aberrant"
