# Generated skeleton — output examples

Short excerpts of **what good generated code looks like**. Use these as the
target voice/structure when writing Step 5 output. The full structural
template (fictitious permits domain) is `assets/case_class_template.py`.

Do **not** invent a second copy of a real case under `volatile/` or elsewhere
and link to it from this skill — keep examples here or in `assets/`.

## Module docstring (business narrative + draft warning)

Write from the interview: why this case exists, what “one” unit of work is,
and any domain constraints that affect the lifecycle. End with the IMPORTANT
block. Do **not** mention the case-designer skill, who generated the file,
scaffold mechanics, or that stub bodies call `_not_implemented` (the call
sites already say that).

```python
"""Indexable file lifecycle for organization MCP file-access servers.

Organizations often expose a read-only MCP server over a client (or project /
work-order / department) file store. That server understands the client's folder
conventions, maps casual names to the right subtree, and searches by filename,
type, and content — typically via vector or graph indexing. Keeping that index
current means treating each source-file path as a long-lived unit of work: when
the store reports a new, changed, or deleted file, something must examine the
bytes, optionally OCR, produce text, summarize, chunk/index, and later tear
down index resources when the file disappears.

``IndexableFileCase`` models that per-path lifecycle. The case's external key is
the native path; there is no "move" (delete + create). ``indexed`` is a resting
state awaiting change or delete; only ``deleted`` is terminal. Textual content
held during processing is ephemeral and must not outlive a successful index
commit.

IMPORTANT: This module is a rough first draft. Expect substantial follow-on work
before production use — refining data structures, implementing trigger/guard/
assertion bodies, tuning ``fsm_state_chains``, and hardening asset contracts.
See ``fsm_state_chains`` on the case class for the lifecycle declaration.
"""
```

### Anti-patterns (do not generate)

```python
# BAD — meta about scaffolding / skill / mechanical stub advice
"""Experiment skeleton — FooCase

Generated via case-designer interview.
Every stub body is one deletable line — return self._not_implemented(...)
Delete _not_implemented once implementations land.

Lifecycle:
  [*] --> new -- ...
"""
```

## Class docstring (short identity)

One or two lines naming the recurring unit of work. This is what `case_doc`
pulls as the briefing lede — keep it crisp; put the story in the module
docstring.

```python
class IndexableFileCase(FolderBackedCase):
    """One source-file path through examine → text → summarize → index, then
    rest until change or delete.
    """
```

## Asset model (purpose + placeholder TODO)

Purpose first (including *why the data exists* — e.g. enough identity to
delete an index row later). Then an explicit TODO that the field layout is a
guess to replace.

```python
class IndexEntryRef(BaseModel):
    """Enough identity to delete one index entry when it becomes invalid.

    Recorded at ``commit_index`` so a later re-index or ``mark_deleted`` can
    remove that row/chunk from the shared index without guessing.

    TODO: replace this placeholder attribute layout with whatever the index
    backend needs to delete precisely (id, collection, namespace, vector key, …).
    """

    entry_id: str = ""
    kind: str = ""  # e.g. "chunk", "summary"
```

## `asset_trust_states` ClassVar (not a pile of module constants)

One frozen mapping on the case class, keyed by alias; each `AssetSpec` reads
from it. Prefer `MappingProxyType` + `frozenset` values.

```python
asset_trust_states: ClassVar[Mapping[str, frozenset[str]]] = MappingProxyType({
    "fingerprint": frozenset({
        "examined",
        "textual_content_available",
        "summarized",
        "indexed",
        "changed",
    }),
    "textual_content": frozenset({
        "textual_content_available",
        "summarized",
    }),
    "summary": frozenset({"summarized", "indexed", "changed"}),
    "index_entries": frozenset({"indexed", "changed"}),
})

asset_aliases = [
    AssetSpec(
        alias="fingerprint",
        relative_path="fingerprint.yaml",
        loader=FileFingerprint,
        states=asset_trust_states["fingerprint"],
        keep=False,
    ),
    # ...
]
```

### Anti-pattern

```python
# BAD — orphan module-level NEEDS_* sets that drift from asset_aliases
NEEDS_FINGERPRINT = {"examined", "indexed", ...}
NEEDS_SUMMARY = {"summarized", "indexed", ...}
```

## Hook signatures — annotate `tctx: EventData`

Import `from transitions.core import EventData` and type every transition hook
argument. `EventData` is the transitions library trigger-context object (not
the case event journal); call kwargs land in `tctx.kwargs`.

```python
from transitions.core import EventData

async def perform_analyze_file(self, tctx: EventData) -> None:
    """TODO(responsibility): ..."""
    return self._not_implemented(None)

async def guard_needs_ocr(self, tctx: EventData) -> bool:
    """TODO(responsibility): ..."""
    return self._not_implemented(False)

async def on_enter_awaiting_review(self, tctx: EventData) -> None:
    """TODO(responsibility): ..."""
    return self._not_implemented(None)
```

Same annotation on `before_` / `after_` / `on_exit_` when those are generated.
`case_assert_*` methods take `ltx`, not `tctx` — leave those as they are.

## `_not_implemented` (method only — not the module header)

Keep scaffolding advice on the helper itself (one line). Do not restate it in
the module docstring.

```python
def _not_implemented(self, retval: Any = None) -> Any:
    """Scaffolding helper for stub hooks; remove with the stubs that call it."""
    caller = sys._getframe(1).f_code.co_qualname
    self.log.warning("STUB not implemented: %s", caller)
    return retval
```
