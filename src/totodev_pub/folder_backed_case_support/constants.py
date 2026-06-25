# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Filesystem-layout constants for the FolderBackedCase family.

Centralized here so every support module agrees on the on-disk names of a case
folder's pieces (record, lease, asset playground, retention manifest)."""

from __future__ import annotations

RECORD_NAME     = "case_record.yaml"
LEASE_NAME      = ".case.lease"      # single-owner lease (§5d): content-free; mtime = "valid-until"
ASSETS_DIR_NAME = "assets"           # the downstream-owned asset "playground"
KEEP_LIST_NAME  = "_keep_assets.txt" # retention manifest at the CASE ROOT (NOT under assets/)
