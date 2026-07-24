# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Best-effort conversion of a (near-)Mermaid sketch into a valid
``fsm_state_chains`` declaration — the ``--convert`` mode of the chain-DSL CLI
(``state_chain_cli``).

The chain DSL already reads most stateDiagram-v2 lines verbatim; what a real
pasted sketch adds is the stuff a whiteboard tolerates and a grammar cannot:
unlabeled edges, free-text labels ("Open Ticket!"), state aliases, notes,
composite states. This module rewrites what it can and comments out what it
cannot, so the output is ALWAYS reviewable text — valid DSL wherever possible,
with ``%% TODO`` markers wherever a human must decide. It never guesses
silently: every rewrite leaves either the original text in a trailing comment
or a note in the returned list.

This is deliberately a TOOL, not parser behavior: accepting "Open Ticket" as a
trigger inside the grammar would mean two spellings of every name. Conversion
is lossy and heuristic, so it emits text the user reviews — the parser's
contract stays exact. Conversion is also idempotent: a line that already parses
as chain DSL passes through untouched, so running ``--convert`` on its own
output is a no-op.

Intake workflow::

    pbpaste | python -m totodev_pub.folder_backed_case_support.state_chain_cli --convert
    # resolve the %% TODO markers, mark manual gates ('==>' / ': =='), re-validate

Supported input beyond the DSL itself: Mermaid stateDiagram-v2 edges (labeled,
unlabeled, labeled boundary hops) and flowchart pipe-label edges
(``A -->|label| B``); ``state "X" as y`` aliases and stereotypes; ``note``
blocks; flowchart node/style declarations. Anything unrecognized is preserved
as a ``%% TODO: unsupported`` comment.
"""

from __future__ import annotations

import re

from totodev_pub.folder_backed_case_support.exceptions import FsmChainParseError
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    StateChainParser,
    _MERMAID_BOILERPLATE_RE,
)

__all__ = ["convert_mermaid_text"]

# A stateDiagram-v2 edge: NAME/[*] arrow NAME/[*], optional `: label`. The dotted and
# thick arrows only occur in flowchart input but are cheap to accept here too.
_SD_EDGE_RE = re.compile(
    r"^(?P<src>\[\*\]|\w+)\s*(?P<arrow>-->|==>|-\.->)\s*(?P<dest>\[\*\]|\w+)"
    r"\s*(?::\s*(?P<label>.*))?$"
)

# A flowchart pipe-label edge: `A -->|label| B` (also thick/dotted arrows).
_PIPE_EDGE_RE = re.compile(
    r"^(?P<src>\w+)\s*(?P<arrow>-->|==>|-\.->)\s*\|\s*(?P<label>[^|]*?)\s*\|\s*(?P<dest>\w+)\s*$"
)

_ALIAS_RE = re.compile(r'^state\s+"(?P<label>[^"]*)"\s+as\s+(?P<id>\w+)\s*$')
_STEREO_RE = re.compile(r"^state\s+(?P<id>\w+)\s*<<\w+>>\s*$")
_COMPOSITE_OPEN_RE = re.compile(r"^state\s+\w+\s*\{\s*$")
_COMPOSITE_CLOSE_RE = re.compile(r"^\}\s*$")
_NOTE_OPEN_RE = re.compile(r"^note\b", re.IGNORECASE)
_NOTE_CLOSE_RE = re.compile(r"^end\s+note\s*$", re.IGNORECASE)
# Flowchart/stateDiagram decoration lines with no FSM meaning.
_DECOR_RE = re.compile(
    r"^(classDef\b|class\b|style\b|linkStyle\b|hide\b|showData\b)", re.IGNORECASE
)
# A flowchart node declaration, e.g. `a["Fancy Label"]`, `b((circle))`, `c{diamond}`.
_NODE_SHAPE_RE = re.compile(r"^(?P<id>\w+)\s*[\[({>]")


def _slug(label: str) -> str:
    """Reduce a free-text Mermaid label to a legal trigger identifier."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()
    if not s:
        s = "step"
    if s[0].isdigit():
        s = "t_" + s
    return s


def _parses_as_dsl(line: str) -> bool:
    try:
        StateChainParser.parse(line)
        return True
    except FsmChainParseError:
        return False


def _convert_edge(m: re.Match, notes: list[str]) -> str:
    src, dest = m.group("src"), m.group("dest")
    arrow = m.group("arrow")
    label = (m.group("label") or "").strip()
    manual = arrow == "==>"
    if arrow == "-.->":
        notes.append(
            f"dotted arrow {src} -.-> {dest} mapped to auto ('-->'); make it "
            "'==>' if it was meant as a manual/event gate"
        )

    # Boundary hops carry no label in the DSL — keep the marker, shed the rest.
    if src == "[*]" or dest == "[*]":
        if label:
            notes.append(
                f"dropped label {label!r} on boundary hop '{src} --> {dest}' "
                "(initial/terminal markers take no label)"
            )
        if manual:
            notes.append(
                f"boundary hop '{src} --> {dest}' uses a plain '-->' (it is a "
                "marker, not a manual edge)"
            )
        return f"{src} --> {dest}"

    dsl_arrow = "==>" if manual else "-->"
    if not label:
        trigger = _slug(f"to_{dest}")
        return f"{src} {dsl_arrow} {dest} : {trigger}   %% TODO: name this trigger"

    candidate = f"{src} {dsl_arrow} {dest} : {label}"
    if _parses_as_dsl(candidate):
        return candidate
    slugged = f"{src} {dsl_arrow} {dest} : {_slug(label)}   %% was \"{label}\""
    return slugged


def convert_mermaid_text(text: str) -> tuple[str, list[str]]:
    """Convert a (near-)Mermaid sketch to chain-DSL text.

    Returns ``(converted_text, notes)``: the cleaned declaration (one line per
    input line kept, `%%` comments preserved) and human-readable notes about
    every transformation that was not a pure passthrough. The converted text is
    expected to PARSE; whether it VALIDATES depends on the sketch (a partial
    sketch may still lack an initial/terminal state) — the caller reports that.
    """
    notes: list[str] = []
    out: list[str] = []
    in_note_block = False

    for raw in text.splitlines():
        line = raw.strip()

        if in_note_block:
            out.append(f"%% {line}")
            if _NOTE_CLOSE_RE.match(line):
                in_note_block = False
            continue

        if not line:
            out.append("")
            continue
        if line.startswith("%%"):
            out.append(line)
            continue
        if _MERMAID_BOILERPLATE_RE.match(line):
            continue                                   # header/direction: silently gone

        # A line that is already valid chain DSL passes through untouched —
        # this is what makes conversion idempotent.
        if _parses_as_dsl(line):
            out.append(line)
            continue

        if _NOTE_OPEN_RE.match(line):
            # Single-line form: `note right of X: text`. Block form runs to `end note`.
            if ":" not in line:
                in_note_block = True
            out.append(f"%% {line}")
            notes.append(f"note preserved as a comment: {line!r}")
            continue

        m = _ALIAS_RE.match(line)
        if m:
            out.append(f'%% state {m.group("id")} was aliased as "{m.group("label")}"')
            notes.append(
                f"state alias dropped: '{m.group('id')}' keeps its identifier; "
                f"display name {m.group('label')!r} kept as a comment"
            )
            continue

        if _STEREO_RE.match(line) or _COMPOSITE_OPEN_RE.match(line) or _COMPOSITE_CLOSE_RE.match(line):
            out.append(f"%% TODO: unsupported construct removed: {line}")
            notes.append(
                f"unsupported stateDiagram construct commented out: {line!r} "
                "(composite/choice/fork states have no DSL equivalent — flatten "
                "the lifecycle into plain states)"
            )
            continue

        if _DECOR_RE.match(line) or _NODE_SHAPE_RE.match(line):
            out.append(f"%% {line}")
            notes.append(f"decoration line preserved as a comment: {line!r}")
            continue

        m = _SD_EDGE_RE.match(line) or _PIPE_EDGE_RE.match(line)
        if m:
            out.append(_convert_edge(m, notes))
            continue

        out.append(f"%% TODO: unsupported line: {line}")
        notes.append(f"unrecognized line commented out: {line!r}")

    # Collapse leading/trailing blank lines for a tidy scaffold.
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out), notes
