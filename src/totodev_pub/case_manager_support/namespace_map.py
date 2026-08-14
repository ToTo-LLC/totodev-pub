# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""The one declaration of the CaseManager on-disk layout, and its renderer.

Why this module exists: the manager namespace is two dozen directories, and until
now its shape could only be learned by reading ``_ensure_namespace_dirs``,
``constants.py`` and the transport together and assembling the tree by hand. A
hand-drawn diagram of two dozen directories drifts, and a drifted map misleads
worse than no map at all — so the tree is *generated* from the same declaration
the manager provisions from.

``CaseManager._ensure_namespace_dirs`` creates exactly the ``DIR`` entries below,
and ``docs/case-manager-layout.md`` is the rendered output. A test asserts both
ends: that a provisioned filespace contains precisely the declared directories,
and that the checked-in document still matches the renderer.

Adding a directory therefore means adding one entry here. Forgetting to document
it is not possible; forgetting to create it fails the test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from totodev_pub.case_manager_support.case_store import QUARANTINED, TERMINATED
from totodev_pub.case_manager_support.constants import (
    EJECT_SUBDIR,
    FLEET_STATUS_FILENAME,
    MANIFEST_FILENAME,
    POLICY_FILENAME,
    QUARANTINE_SUBDIR,
    RESULTS_SUBDIR,
    TERMINATION_SUBDIR,
)

if TYPE_CHECKING:
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

#: Stands in for a case id in a path created one-per-case at runtime. Named a
#: "token" rather than a placeholder because that word is reserved for the cache
#: placeholder file, and ``test_no_storage_mechanics_leak_out_of_the_store``
#: enforces the reservation by substring.
CASE_KEY_TOKEN = "{case_id}"


class EntryKind(Enum):
    """How an entry comes to exist, which decides what a test may assert about it.

    ``DIR`` is provisioned eagerly and is the only kind a freshly created
    filespace is required to contain. ``RUNTIME_DIR`` is created on demand — per
    case, or per status bucket on first arrival — so asserting its presence at
    provisioning time would be asserting a fiction. ``FILE`` is written by the
    running manager.
    """

    DIR = "dir"
    RUNTIME_DIR = "runtime_dir"
    FILE = "file"


@dataclass(frozen=True)
class LayoutEntry:
    """One path in the layout, with the annotation the rendered map carries."""

    path: str
    purpose: str
    kind: EntryKind = EntryKind.DIR


def storage_entries(policy: "CaseManagerPolicy") -> tuple[LayoutEntry, ...]:
    """The status buckets: case data, owned by the store rather than the manager.

    Only the live bucket is provisioned eagerly (``LocalCaseStore`` creates it
    with the root). The other two appear when a case first reaches them, which is
    why they are ``RUNTIME_DIR`` — a filespace that has never terminated a case
    genuinely has no ``terminated/``.
    """
    live = "CASE DATA — cases being worked on. Stable path per case"
    done = "CASE DATA — finished cases, in date folders"
    broken = "CASE DATA — cases set aside for a human, in date folders"
    return (
        LayoutEntry(policy.live_bucket, live),
        LayoutEntry(TERMINATED, done, EntryKind.RUNTIME_DIR),
        LayoutEntry(QUARANTINED, broken, EntryKind.RUNTIME_DIR),
    )


def namespace_entries(policy: "CaseManagerPolicy") -> tuple[LayoutEntry, ...]:
    """Everything under the manager namespace, parent before child.

    Paths are relative to the manager directory (``cache_root/.case_manager`` by
    default) and always POSIX-separated: they are protocol, not local paths. The
    ordering is load-bearing — the renderer derives tree depth and sibling
    position from it.

    None of this is case data. The scratch docks are the exception that proves
    the rule: a folder in ``staging/`` or ``adopt_drop/`` is a case being
    assembled, and it leaves the namespace the moment it is admitted.
    """
    return (
        *_namespace_files(),
        *_scratch_docks(policy),
        *_mailbox_entries(policy),
        *_ticket_entries(),
    )


def _namespace_files() -> tuple[LayoutEntry, ...]:
    return (
        LayoutEntry(
            POLICY_FILENAME,
            "Layout and tunables. Written once, at creation",
            EntryKind.FILE,
        ),
        LayoutEntry(
            MANIFEST_FILENAME,
            "Heartbeat and protocol paths, for clients",
            EntryKind.FILE,
        ),
        LayoutEntry(
            FLEET_STATUS_FILENAME,
            "One row per case, for cheap whole-fleet reads",
            EntryKind.FILE,
        ),
    )


def _scratch_docks(policy: "CaseManagerPolicy") -> tuple[LayoutEntry, ...]:
    in_process = "Scratch dock for in-process assembly. Holds a lease while building"
    handover = "Scratch dock for a folder handed over by another process. No lease"
    return (
        LayoutEntry(policy.staging_subdir, in_process),
        LayoutEntry(policy.adopt_drop_subdir, handover),
    )


def _mailbox_entries(policy: "CaseManagerPolicy") -> tuple[LayoutEntry, ...]:
    """The four request mailboxes plus the shared results directory.

    The stage vocabulary is inconsistent by accident rather than design:
    ``firing``, ``executing`` and adopt's ``pending`` all mean "in progress",
    while fire's ``pending`` means "accepted, not yet launched". Recorded as it
    is — the map's job is to describe the tree, not to improve it.
    """
    key = CASE_KEY_TOKEN
    fire, adopt = policy.fire_mailbox_subdir, policy.adopt_mailbox_subdir
    reclassify, shutdown = (
        policy.reclassify_mailbox_subdir,
        policy.shutdown_mailbox_subdir,
    )
    unparseable = "Dead-lettered: the request would not parse"
    return (
        LayoutEntry(fire, "Mailbox: 'make this case take a step'"),
        LayoutEntry(f"{fire}/intake", "Submitted fires, awaiting the next drain"),
        LayoutEntry(f"{fire}/malformed", unparseable),
        LayoutEntry(
            f"{fire}/pending",
            "Accepted, awaiting a slot or choke permit",
            EntryKind.RUNTIME_DIR,
        ),
        LayoutEntry(f"{fire}/pending/{key}", "One case's queued step", EntryKind.RUNTIME_DIR),
        LayoutEntry(f"{fire}/firing", "Launched by the sweep", EntryKind.RUNTIME_DIR),
        LayoutEntry(f"{fire}/firing/{key}", "One case's running step", EntryKind.RUNTIME_DIR),
        LayoutEntry(adopt, "Mailbox: 'take ownership of this folder'"),
        LayoutEntry(f"{adopt}/intake", "Submitted adopts, awaiting the next drain"),
        LayoutEntry(f"{adopt}/pending", "Executing now. Settled against the store after a crash"),
        LayoutEntry(reclassify, "Mailbox: 'change this case to a different type'"),
        LayoutEntry(f"{reclassify}/intake", "Submitted reclassifies, awaiting the next drain"),
        LayoutEntry(f"{reclassify}/malformed", unparseable),
        LayoutEntry(f"{reclassify}/executing", "Executing now, one subfolder per case"),
        LayoutEntry(f"{reclassify}/executing/{key}", "One case's step", EntryKind.RUNTIME_DIR),
        LayoutEntry(shutdown, "Mailbox: 'stop'. Polled by the host, not the adapter"),
        LayoutEntry(f"{shutdown}/intake", "Any non-hidden file here requests a shutdown"),
        LayoutEntry(RESULTS_SUBDIR, "Answers by correlation id. Swept after result_ttl_secs"),
    )


def _ticket_entries() -> tuple[LayoutEntry, ...]:
    """Internal job files for work that must outlive a crash.

    Note the near-collision with the status buckets: ``quarantine/`` here holds a
    *job file* naming a case, while ``quarantined/`` outside holds the case data
    itself. One letter apart, completely different contents.
    """
    tickets = (
        (TERMINATION_SUBDIR, "termination"),
        (EJECT_SUBDIR, "ejection out of storage"),
        (QUARANTINE_SUBDIR, "quarantine"),
    )
    entries: list[LayoutEntry] = []
    for subdir, what in tickets:
        entries.append(LayoutEntry(subdir, f"Tickets for {what}. Job files, never case data"))
        entries.append(LayoutEntry(f"{subdir}/pending", "Owed, and will be retried"))
        entries.append(
            LayoutEntry(f"{subdir}/failed", "Gave up after max retries. A human must look")
        )
    return tuple(entries)


def provisioned_namespace_dirs(policy: "CaseManagerPolicy") -> tuple[str, ...]:
    """The directories ``_ensure_namespace_dirs`` must create, parent before child.

    Relative to the manager directory. This is the *only* list the manager
    provisions from, so the tree it creates cannot disagree with the rendered map.
    """
    return tuple(e.path for e in namespace_entries(policy) if e.kind is EntryKind.DIR)


def namespace_dirs_on_disk(manager_dir: Path) -> tuple[str, ...]:
    """Every directory actually present under ``manager_dir``, POSIX-relative, sorted.

    The other half of the drift test: compared against
    ``provisioned_namespace_dirs`` so a directory created outside the declaration
    is caught rather than quietly accumulating.
    """
    if not manager_dir.is_dir():
        return ()
    found = (p for p in manager_dir.rglob("*") if p.is_dir())
    return tuple(sorted(p.relative_to(manager_dir).as_posix() for p in found))


def is_declared_dir(path: str, policy: "CaseManagerPolicy") -> bool:
    """Whether a POSIX-relative directory path is one the declaration accounts for.

    ``{case_id}`` in a declared path matches any single path segment, which is
    what lets a per-case runtime directory be recognised without the declaration
    having to know case ids.
    """
    for entry in namespace_entries(policy):
        if entry.kind is EntryKind.FILE:
            continue
        pattern = "^" + re.escape(entry.path).replace(re.escape(CASE_KEY_TOKEN), "[^/]+") + "$"
        if re.match(pattern, path):
            return True
    return False


def undeclared_dirs(manager_dir: Path, policy: "CaseManagerPolicy") -> tuple[str, ...]:
    """Directories present under ``manager_dir`` that the declaration does not cover.

    The check that matters after the manager has actually *run*, rather than only
    been provisioned. Provisioning is not the only thing that creates directories
    — the transport's ``ensure_dirs`` and the per-case stage moves do too — so a
    map validated only at provisioning time can still be incomplete in practice.

    Empty is the passing answer.
    """
    return tuple(p for p in namespace_dirs_on_disk(manager_dir) if not is_declared_dir(p, policy))


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------

#: Column the purpose annotation starts at. A path longer than this simply pushes
#: its own annotation right rather than truncating.
_ANNOTATION_COLUMN = 46


def render_layout_map(policy: "CaseManagerPolicy") -> str:
    """Render the whole layout as an annotated tree, rooted at ``cache_root``.

    The output is what ``docs/case-manager-layout.md`` carries between its
    generated markers, and a test compares the two. Regenerate rather than
    editing the document by hand.
    """
    namespace_purpose = "The manager's own workspace. Not a status bucket, not case data"
    lines = ["cache_root/"]
    for entry in storage_entries(policy):
        lines.append(_line("", entry.path + "/", entry, last=False))
    root = LayoutEntry(policy.manager_namespace, namespace_purpose)
    lines.append(_line("", policy.manager_namespace + "/", root, last=True))
    lines.extend(_render_subtree(namespace_entries(policy)))
    return "\n".join(lines)


def _render_subtree(entries: tuple[LayoutEntry, ...]) -> list[str]:
    """Render a parent-before-child entry list as a box-drawing tree.

    Depth comes from the path. An entry is "last" when no later entry shares its
    parent, and an ancestor contributes a continuation bar only while it still
    has siblings to come — without that check the final group draws a dangling
    ``│`` down its left edge.
    """
    lines: list[str] = []
    for position, entry in enumerate(entries):
        later = entries[position + 1 :]
        parts = entry.path.split("/")
        prefix = "    " + "".join(
            "│   " if _has_later_sibling("/".join(parts[:depth]), later) else "    "
            for depth in range(1, len(parts))
        )
        last = not _has_later_sibling(entry.path, later)
        lines.append(_line(prefix, parts[-1] + _suffix(entry), entry, last=last))
    return lines


def _has_later_sibling(path: str, later: tuple[LayoutEntry, ...]) -> bool:
    parent = _parent_of(path)
    return any(_parent_of(entry.path) == parent for entry in later)


def _parent_of(path: str) -> str:
    return "/".join(path.split("/")[:-1])


def _suffix(entry: LayoutEntry) -> str:
    return "" if entry.kind is EntryKind.FILE else "/"


def _line(prefix: str, name: str, entry: LayoutEntry, *, last: bool) -> str:
    """One tree line: connector, name, then the purpose in an aligned column."""
    drawn = f"{prefix}{'└── ' if last else '├── '}{name}"
    marker = "  (on demand)" if entry.kind is EntryKind.RUNTIME_DIR else ""
    return f"{drawn.ljust(_ANNOTATION_COLUMN)}{entry.purpose}{marker}".rstrip()


# ----------------------------------------------------------------------
# Document maintenance
# ----------------------------------------------------------------------

BEGIN_MARKER = "<!-- BEGIN GENERATED LAYOUT -->"
END_MARKER = "<!-- END GENERATED LAYOUT -->"


def replace_generated_block(document: str, tree: str) -> str:
    """Return ``document`` with the text between the layout markers set to ``tree``.

    Pure and idempotent, so the test that checks the document is current and the
    command that refreshes it share one implementation instead of two that can
    disagree. The prose around the markers is hand-written and untouched.

    Raises ``ValueError`` when either marker is missing or they are out of order —
    silently appending a second block would leave two trees in one document, one
    of them wrong.
    """
    start, end = document.find(BEGIN_MARKER), document.find(END_MARKER)
    if start < 0 or end < 0:
        raise ValueError(f"document is missing {BEGIN_MARKER!r} or {END_MARKER!r}")
    if end < start:
        raise ValueError(f"{END_MARKER!r} appears before {BEGIN_MARKER!r}")
    head = document[: start + len(BEGIN_MARKER)]
    return f"{head}\n```text\n{tree}\n```\n{document[end:]}"


def _main(argv: list[str]) -> int:
    """Print the tree, or rewrite the generated block of the document at ``argv[0]``."""
    from totodev_pub.case_manager_support.case_manager_policy import CaseManagerPolicy

    tree = render_layout_map(CaseManagerPolicy())
    if not argv:
        print(tree)
        return 0
    target = Path(argv[0])
    updated = replace_generated_block(target.read_text(encoding="utf-8"), tree)
    target.write_text(updated, encoding="utf-8")
    print(f"Updated the generated layout block in {target}")
    return 0


if __name__ == "__main__":  # pragma: no cover - developer entry point
    import sys

    raise SystemExit(_main(sys.argv[1:]))
