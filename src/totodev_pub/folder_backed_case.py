# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
FolderBackedCase: folder-anchored, file-first case lifecycle framework.

OVERVIEW
A case is a heavyweight, FSM-driven object whose entire state—record,
event log, and working files—lives in a single folder on disk. No
database required. Subclass ``FolderBackedCase`` to define a case type.
**IMPORTANT**: Read ``FolderBackedCaseInterface`` first to understand
how to subclass and use this class.

While ``FolderBackedCaseInterface`` provides the basic-usage contract,
this class implements the core mechanics and advanced methods which you
can find in commented SECTION 3 and SECTION 4 below.

Core pieces (for case authors)
--------------------------
CaseRecord              — skinny Pydantic identity card (case_record.yaml).
CaseEventJournalView    — read-only facade over the case event-log protocol.
CaseAssets              — working-file playground + retention manifest (_keep.txt).
FolderBackedCaseInterface — basic-usage contract (read this first).
FolderBackedCase        — ABC you subclass to define a case type.
AdvanceResult           — outcome of case_advance() (non-throwing reporter).
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import logging
import weakref
from pathlib import Path
from typing import Any

from totodev_pub.folder_backed_case_support.folder_backed_case_interface import (
    FolderBackedCaseInterface, _raises_when_detached,
)
from totodev_pub.folder_backed_case_support.constants import (
    RECORD_NAME, LEASE_NAME, LOGS_DIR_NAME, LOG_FILE_NAME, ASSETS_DIR_NAME,
    CASE_RESERVED_ARTIFACT_NAMES, CASE_BASE_EVENT_PREFIX,
    DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS,
    DEFAULT_LEASE_TTL_SECS, LEASE_HEARTBEAT_THROTTLE_SECS,
)
from totodev_pub.folder_backed_case_support.helpers import (
    _utcnow, _local_mtime_as_utc, _norm_rel,
)
from totodev_pub.folder_backed_case_support.case_id_generation import (
    CaseIDGenerator, TimeSlugCaseIDGenerator, DEFAULT_CASE_ID_GENERATOR,
)
from totodev_pub.folder_backed_case_support.exceptions import (
    CaseAlreadyOpenError, OwnershipLostError, DetachedCaseError,
    CaseTypeMismatchError, RecordTypeMismatchError,
    IncompatibleReclassError, MissingFsmError, FsmChainParseError, FsmBindingError,
    AutoAdvanceBlocked, TriggerTimeout, MissingAssetSchemaError, MissingTriggerChokesError,
    CaseTransitionInFlightError,)
from totodev_pub.folder_backed_case_support.asset_schema import AssetSpec
from totodev_pub.folder_backed_case_support.aliased_asset_specs import AliasedAssetSpecs
from totodev_pub.folder_backed_case_support.case_type_spec import CaseTypeSpec
from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.case_journal import CaseEventJournal, CaseEventJournalView
from totodev_pub.folder_backed_case_support.case_assets import CaseAssets
from totodev_pub.folder_backed_case_support.case_keep_manifest import CaseKeepManifest
from totodev_pub.folder_backed_case_support.advance_result import AdvanceResult
from totodev_pub.folder_backed_case_support.case_advancer import _CaseAdvancer
from totodev_pub.folder_backed_case_support.heartbeat_lease import (
    HeartbeatLease, LeaseAlreadyHeldError, LeaseOwnershipLostError,)
from totodev_pub.folder_backed_case_support.state_chain_parser import (
    StateChainParser, FsmChainSpec,)
from totodev_pub.folder_backed_case_support.case_machine_factory import _CaseMachineFactory
from totodev_pub.folder_backed_case_support.case_logging import (
    LogRetention, set_case_log_retention,
    build_case_logger, write_attach_banner, write_detach_banner, disable_case_file_tee,
)
from totodev_pub.folder_backed_case_support.case_read_protocol import CaseReadProtocol


logger = logging.getLogger(__name__)

__all__ = [
    "FolderBackedCase", "FolderBackedCaseInterface", "CaseReadProtocol", "AssetSpec",
    "CaseRecord", "CaseEventJournalView", "CaseAssets", "AdvanceResult",
    "FsmChainSpec", "CaseTypeSpec", "CaseAlreadyOpenError", "OwnershipLostError",
    "DetachedCaseError", "CaseTypeMismatchError",
    "RecordTypeMismatchError", "IncompatibleReclassError", "MissingFsmError",
    "FsmChainParseError", "FsmBindingError", "AutoAdvanceBlocked", "TriggerTimeout",
    "MissingAssetSchemaError", "MissingTriggerChokesError",
    "CaseIDGenerator", "TimeSlugCaseIDGenerator",
    "CASE_RESERVED_ARTIFACT_NAMES", "CASE_BASE_EVENT_PREFIX",
    "LogRetention", "set_case_log_retention",
]

# ---------------------------------------------------------------------------
# FolderBackedCase — the logic base class
# ---------------------------------------------------------------------------

class FolderBackedCase(FolderBackedCaseInterface):
    """Implementation of folder-backed case types.

    Subclass this ABC (not ``FolderBackedCaseInterface`` directly). Contract
    docs for basic-usage members live on ``FolderBackedCaseInterface`` and are
    inherited. This class is organized in four labeled sections:

      * SECTION 1 — START HERE: declaration attributes + hook naming
        (contract docs on the interface).
      * SECTION 2 — Runtime API (contract docs on the interface).
      * SECTION 3 — Advanced customization seams.
      * SECTION 4 — Internal mechanics (maintainers only).

    Built on the ``transitions`` library; this class commits to ongoing use of
    that library. Advanced ``transitions`` customization is possible but beyond
    this docstring.
    """

    # =======================================================================
    # SECTION 1 — START HERE: define your case type (ALL audiences)
    # -----------------------------------------------------------------------
    # Declaration attributes (fsm_state_chains, fsm_trigger_chokes,
    # asset_aliases, flexible_asset_alias_loading) and their contract docs live
    # on FolderBackedCaseInterface. Simple case types need ONLY those plus a
    # few perform_<trigger> / guard_<guard> methods. Rarer define-time seams —
    # `_record_cls`, `compile_fsm()` — live in SECTION 3.
    # =======================================================================

    _asset_book: AliasedAssetSpecs | None = None

    # ---- Hook & guard naming (see FolderBackedCaseInterface class docstring) ----
    #
    # After `fsm_state_chains`, behavior is attached by METHOD NAME. Suffixes must
    # match parsed state/trigger/guard names exactly. Orphan hook methods (no matching
    # DSL name) fail at bind via validate_object_compatibility(orphan_detection="error").
    #
    # SIGNATURE: every hook takes `tctx` after `self`. Hooks must yield the event loop;
    # offload blocking work via case_run_blocking(). See "Creating Hook Methods" in the
    # FolderBackedCaseInterface class docstring for the well-behaved-async /
    # lease-keepalive contract.

    # =======================================================================
    # SECTION 2 — Quick-start runtime API (mainstream "quick & dirty" users)
    # -----------------------------------------------------------------------
    # Contract docs for every member below live on FolderBackedCaseInterface
    # (inherited via docstring inheritance). Most users need nothing beyond
    # Sections 1 and 2.
    # =======================================================================

    @classmethod
    def create_case_in_folder(
        cls,
        case_folder: Path,
        *,
        case_id: str | CaseIDGenerator | None = None,
        external_key: str | None = None,
        nickname: str | None = None,
        **fields,
    ) -> FolderBackedCase:
        # Planned CaseManager (draft: notebooks/DEVDAVE/case_manager_classes/
        # CaseManager Model.md) will also call this for fleet inception.
        case_folder = Path(case_folder)
        book = cls._resolve_asset_book()
        asset_aliases = book.to_record()
        parent = case_folder.parent
        if not parent.exists():
            raise FileNotFoundError(
                f"Parent folder {parent} does not exist. "
                "Create/confirm the parent folder first, then retry create_case_in_folder()."
            )
        if not parent.is_dir():
            raise NotADirectoryError(
                f"Parent path {parent} exists but is not a directory."
            )
        if case_folder.exists():
            if not case_folder.is_dir():
                raise NotADirectoryError(
                    f"Target path {case_folder} exists but is not a directory."
                )
        else:
            case_folder.mkdir(parents=False, exist_ok=False)
        reserved_paths = {
            name: case_folder / name for name in CASE_RESERVED_ARTIFACT_NAMES
        }
        found = [name for name, path in reserved_paths.items() if path.exists()]
        if found:
            found_list = ", ".join(sorted(found))
            raise FileExistsError(
                f"Cannot create a new case in {case_folder}: existing case artifacts "
                f"found ({found_list}). Choose a clean case folder."
            )
        if case_id is None:
            case_id = cls.case_id_generator.generate(case_cls=cls)
        elif isinstance(case_id, CaseIDGenerator):
            case_id = case_id.generate(case_cls=cls)
        record = cls._record_cls(
            case_object_type=cls.__name__,
            case_id=case_id,
            external_key=external_key,
            nickname=nickname,
            created=_utcnow(),
            asset_aliases=asset_aliases,
            fsm_state_chains=list(cls.fsm_state_chains),
            **fields,
        )
        # Direct save (no instance yet) — SAFE BY CONSTRUCTION: case_object_type is set to
        # cls.__name__ here, so it satisfies the _flush_record type-name guard.
        record.save(str(case_folder / RECORD_NAME))
        case = cls(case_folder)
        cls._seed_keep_rules(case._keep_manifest)
        case._journal.log_created(
            cls.__name__,
            case_id=record.case_id,
            external_key=record.external_key,
        )
        case._journal.log_state_entered(cls._fsm.initial_state)
        return case

    # Re-opening an existing case by its concrete class is the constructor `cls(folder)`;
    # re-opening WITHOUT knowing the class is case_type_registry.rehydrate(folder). Both
    # are documented in SECTION 4 (__init__) and the CaseTypeRegistry, respectively.

    def case_detach(self) -> None:
        # Guarded on is_active() (not just "is not None"), so this runs exactly ONCE per
        # live attach — idempotent against a repeat explicit call, __del__ calling it
        # again, or mid-reclassify's internal call. Banner + tee-disable happen BEFORE
        # release(): we must still legitimately own the folder to write either.
        #
        # Always written, even on a terminal case: attach/detach banners are SESSION
        # markers for this in-memory instance, not a claim about the log FILE's
        # lifecycle. A terminal case can still be legitimately rehydrated later (nothing
        # gates __init__ on case_is_terminal), and that rehydrate unconditionally writes
        # its own attach banner — so pretending detach could "reseal" a purged file was
        # already false. Keeping both bookends symmetric and unconditional is simpler and
        # doesn't claim a guarantee ("never touched again") the code can't actually make.
        if self._lease is not None and self._lease.is_active():
            write_detach_banner(self.log)
            disable_case_file_tee(self.log)
            self._lease.release()

    @_raises_when_detached
    async def case_advance(
        self, trigger: str | None = None, trigger_kwargs: dict | None = None,
    ) -> AdvanceResult:
        # Full advance contract: see `_CaseAdvancer.advance`.
        return await _CaseAdvancer(self).advance(trigger, trigger_kwargs)

    # Why no run_to_completion()/drive loop lives here: see `_CaseAdvancer.advance`.

    # ---- Identity & status (read-only snapshots) ----

    @property
    def case_id(self) -> str:
        return self._record.case_id

    @property
    def case_external_key(self) -> str | None:
        return self._record.external_key

    @property
    def case_folder(self) -> Path:
        return self._folder

    @property
    def case_state(self) -> str:
        return self._case_state

    @property
    def case_terminal_states(self) -> frozenset[str]:
        # Prefer this over digging into `_fsm`; full compiled contract is still
        # `case_type_spec().fsm`.
        return frozenset(self._fsm.terminal_states)

    @property
    def case_is_live(self) -> bool:
        return self.case_state not in self.case_terminal_states

    @property
    def case_is_terminal(self) -> bool:
        return self.case_state in self.case_terminal_states

    @property
    def case_is_detached(self) -> bool:
        return self._lease is None or not self._lease.is_active()

    @property
    def case_is_advanceable(self) -> bool:
        # See `_CaseAdvancer.is_advanceable`.
        return _CaseAdvancer(self).is_advanceable

    @property
    def case_was_blocked(self) -> bool:
        # Sticky last-observation flag; set/cleared by `_CaseAdvancer` and
        # `_on_prepare_fsm_event` (direct trigger clear). See interface docstring.
        return self._was_blocked

    @property
    def case_transition_fail_count(self) -> int:
        # Derivation: see `CaseEventJournal.count_fails_this_dwell`.
        return self._journal.count_fails_this_dwell()

    @property
    def case_dwell_secs(self) -> float:
        # Override SEAM: a subclass may override this property (e.g. to fake the
        # clock in tests); `_CaseMachineFactory` reads it back for `@DWELL`.
        return (_utcnow() - self._state_entered_at).total_seconds()

    @property
    def case_last_event_at(self) -> datetime.datetime | None:
        return _local_mtime_as_utc(self._journal.last_activity_at) or self._record.created

    @property
    def case_events(self) -> CaseEventJournalView:
        # Writes go through CaseEventJournal, not this view.
        return self._journal.view()

    # ---- assets (playground + retention), grouped on CaseAssets ----

    @property
    def case_assets(self) -> CaseAssets:
        # Kept off this class's own namespace so asset concerns stay grouped in
        # one place. For non-asset files (or a runtime keep decision at any scope,
        # assets included), use `case_keep_files()` instead.
        return self._assets

    def case_keep_files(self, *patterns: str | Path) -> None:
        self._keep_manifest.add_rules(*patterns)

    def case_load_asset(self, alias: str) -> object:
        type(self)._resolve_asset_book().assert_trusted(alias, self.case_state)
        return self.case_assets.load_dataclass(alias)

    # ---- record read accessor ----

    def case_record(self, *, force: bool = False) -> CaseRecord:
        self._record.reload_from_file(force=force)
        return self._record.detached_copy()

    # ---- operator alert channel (type-agnostic escalation marker) ----

    def case_log_alert(self, short_msg: str = "", *, where: str | None = None) -> None:
        self._journal.log_alerted(
            where or self.case_state, msg=short_msg
        )

    # ---- folder peek: read a case folder without constructing a live instance ----

    @staticmethod
    def peek_case_record(
        folder: Path,
        *,
        record_cls: type[CaseRecord] | None = None,
        case_cls: type[FolderBackedCase] | None = None,
    ) -> CaseRecord:
        if record_cls is None:
            record_cls = case_cls._record_cls if case_cls is not None else CaseRecord
        # Peek is explicitly lock-free; the case lease is not ours to take here.
        return record_cls.open(str(Path(folder) / RECORD_NAME), without_lock=True)

    @staticmethod
    def peek_case_events(folder: Path) -> CaseEventJournalView:
        return CaseEventJournalView.for_folder(Path(folder))

    @staticmethod
    def peek_case_assets(folder: Path, *, resolve_asset_types: bool = False) -> CaseAssets:
        record = CaseRecord.open(str(Path(folder) / RECORD_NAME), without_lock=True)
        specs = AliasedAssetSpecs.from_record(
            record.asset_aliases, resolve_types=resolve_asset_types
        ).spec_map()
        return CaseAssets(
            Path(folder), asset_specs=specs, flexible_asset_alias_loading=True,
        )

    @staticmethod
    def is_heartbeat_expired(folder: Path) -> bool | None:
        # Return-value semantics: see `HeartbeatLease.is_expired`
        # (on ``folder / LEASE_NAME``).
        return HeartbeatLease.is_expired(Path(folder) / LEASE_NAME)

    @staticmethod
    def peek_lease_secs_left(folder: Path) -> float | None:
        # Return-value semantics: see `HeartbeatLease.secs_left`
        # (on ``folder / LEASE_NAME``).
        return HeartbeatLease.secs_left(Path(folder) / LEASE_NAME)

    @staticmethod
    def get_case_reader(folder: Path) -> "FolderBackedCaseReader":
        from totodev_pub.folder_backed_case_reader import FolderBackedCaseReader
        return FolderBackedCaseReader(Path(folder))

    # =======================================================================
    # SECTION 3 — Customization seams (highly-custom developers)
    # -----------------------------------------------------------------------
    # Overridable hooks and policy knobs for the peculiar use case. Everything
    # here ships with a sensible default; override only what you need. None of
    # this is required for the mainstream path in Section 2.
    # =======================================================================

    # ---- Define-time seams (rarely needed; most case types use the defaults) ----

    # The record-type seam: defaults to CaseRecord. Override with a CaseRecord subclass
    # when extra fields are needed. Read back via case_record().
    _record_cls: type[CaseRecord] = CaseRecord

    @classmethod
    def case_type_spec(cls) -> CaseTypeSpec:
        """Compiled class-behavior contract: FSM (including chokes) + asset aliases.

        Class-level only — the same for every instance of this type. Pool drivers,
        validators, and tooling should use this rather than private ``_fsm`` /
        ``_asset_book``."""
        return CaseTypeSpec(fsm=cls._fsm, assets=cls._resolve_asset_book())

    @classmethod
    def _resolve_asset_book(cls) -> AliasedAssetSpecs:
        """The class's declared alias book (built once in ``__init_subclass__``)."""
        assert cls._asset_book is not None  # populated by __init_subclass__
        return cls._asset_book

    @classmethod
    def _seed_keep_rules(cls, keep_manifest: CaseKeepManifest) -> None:
        """Append keep rules for every declaration with keep=True (idempotent).

        Writes directly into the case-root manifest, applying the ``assets/`` prefix
        HERE — retention is a case-level decision, not something CaseAssets manages on
        its own behalf (see the ``CaseAssets`` class docstring)."""
        paths = [
            spec.relative_path
            for spec in cls._resolve_asset_book().spec_map().values()
            if spec.keep
        ]
        if paths:
            keep_manifest.add_rules(*(f"{ASSETS_DIR_NAME}/{_norm_rel(p)}" for p in paths))

    @classmethod
    def _require_fsm_trigger_chokes_declared(cls) -> None:
        """Every subclass must set `fsm_trigger_chokes` explicitly ({} is valid)."""
        if cls.fsm_trigger_chokes is None:
            raise MissingTriggerChokesError(cls.__name__)

    @classmethod
    def _fold_trigger_chokes(cls, spec: FsmChainSpec) -> FsmChainSpec:
        """Merge this class's `fsm_trigger_chokes` into a compiled spec. Unknown trigger
        keys are a build-time error. Override authors should call this after building
        their hand-crafted spec unless they populate `trigger_chokes` themselves."""
        raw = cls.fsm_trigger_chokes
        if not raw:
            return spec
        known = set(spec.triggers)
        folded: dict[str, frozenset[str]] = {}
        for trigger, resources in raw.items():
            if trigger not in known:
                raise FsmChainParseError(
                    f"fsm_trigger_chokes names trigger {trigger!r}, which is not among "
                    f"this class's FSM triggers ({sorted(known)!r})"
                )
            if not isinstance(resources, (set, frozenset, list, tuple)):
                raise FsmChainParseError(
                    f"fsm_trigger_chokes[{trigger!r}] must be a set of resource name "
                    f"strings, not {type(resources).__name__}"
                )
            names = frozenset(str(name) for name in resources)
            if not names:
                continue
            if trigger in folded:
                raise FsmChainParseError(
                    f"fsm_trigger_chokes declares trigger {trigger!r} more than once"
                )
            folded[trigger] = names
        spec.trigger_chokes = folded
        return spec

    @classmethod
    def compile_fsm(cls) -> FsmChainSpec:
        """Render this class's declared FSM into an FsmChainSpec.

        Quick use:
          You almost never call or override this. The default simply parses
          `fsm_state_chains` for you; just declare your chains and move on.

        Advanced:
          This is the single, unambiguous manual escape hatch. OVERRIDE it to build
          or extend the spec by hand for the rare cases the chain DSL can't express
          (arbitrary callbacks, `unless`, state objects) — optionally by parsing the
          chains first and then tweaking the result. An override OWNS whether and when
          to call validate() / expand_wildcards() / classify() / apply_implicit_fail_cap().

        Maintainer notes:
          PURE: touches no class state and is called exactly once per subclass (by
          __init_subclass__), whose job is to cache the result as the `_fsm` singleton.
          The default parses `fsm_state_chains`, runs the whole-graph
          FsmChainSpec.validate(), then injects any `*--...-->` wildcard edges via
          expand_wildcards() (in that order, so the typo checks see only the explicit
          graph)."""
        return cls._fold_trigger_chokes(
            StateChainParser.parse(cls.fsm_state_chains)
            .validate()
            .expand_wildcards()
            .classify()
            .apply_implicit_fail_cap()
        )

    # ---- Overridable event handlers (on_<event> lifecycle notifications) ----
    # Fired by the framework at well-defined lifecycle moments. All default to
    # no-op; override only the ones your case type needs.  These provide an 
    # an opportunity to handle trigger exceptions or do pre-closeout handling.


    def on_transition_exception(self, begin_state, trigger, final_state, exc) -> None:
        """Overridable recovery hook, fired (before the exception re-raises) whenever a
        transition's dispatch raised. Default: no-op.

        Compare `begin_state` to `final_state` to tell which half of the dispatch failed:
          `begin_state == final_state` ⇒ a PRE-commit failure — a guard or the trigger's
          own work (`before`/`perform_<trigger>`) raised, the case never left its state
          (the retryable "no progress" kind). `begin_state != final_state` ⇒ a POST-commit
          failure — an `on_exit`/`on_enter`/`after` hook raised AFTER the state already
          changed; the case is in `final_state` carrying the baggage of a failed
          side-effect.

        Advanced:
          Use it to compensate from inside the case (which, unlike a generic driver, knows
          its own data): mark a record field, schedule a fix-up, set a flag a later guard
          reads.

          DO NOT fire a transition from within this hook — re-entering the machine
          mid-dispatch is unsupported. To route to a fault state, prefer the declarative
          `@FAIL>=n` divert edge, or record intent here and let the next case_advance() carry
          it out."""

    def on_terminating(self) -> None:
        """Overridable hook fired in phase 1 (pre-finalization): assets still exist,
        record not yet stamped. Override to retain final artifacts before the
        ephemeral purge — a RUNTIME keep decision that can't be made declaratively via
        ``AssetSpec(keep=True)`` (e.g. "keep the draft only if it got this far"). Call
        ``case_keep_files()`` with the full case-relative path (``"assets/..."`` for an
        asset, since this method is case-root scoped, not assets-scoped). Default:
        no-op. Heavy async work belongs in an async ``before_`` hook on the
        terminating transition; this hook is synchronous."""

    # ---- Extended-status hook (polled, not event-driven) ----

    def case_ext_status_info(self) -> dict[str, Any]:
        """Overridable hook for extended status when a case runs under ``CaseManager``.

        The manager periodically publishes a fleet-status board — a shared snapshot of
        all in-pool cases for operators and clients. On each row build it calls this
        method and merges the returned dict into that row's ``ext`` field. Override to
        supply case-specific extended status (e.g. ``percent_complete`` while a long,
        slow ``perform_*`` step runs) without persisting transient progress to disk.
        Default: no-op (empty dict).

        Quick use:
          Surface transient, in-memory progress from inside a running step — e.g. read
          an attribute a perform_* trigger updates as it works, and return it here.
          Typically only meaningful while the case is sitting in one particular state;
          return {} the rest of the time.

          Must return a JSON-serializable dict[str, Any]. Any exception, non-dict
          return, or unserializable value is logged (throttled per case) and treated
          as {} — the board only degrades, it never fails or stalls the beat because
          of this hook.

        Advanced:
          Keep this FAST and CHEAP — it may be called on every row build (as often as
          once per maintenance tick), so never touch disk, never block, never await.
          It may also be called WHILE a perform_* trigger for this case is actively
          running (the fleet writer runs outside the trigger's own execution), so
          reading a value mid-update is expected and fine: this hook has no
          consistency guarantee relative to an in-flight trigger, and generally
          shouldn't need one for a best-effort progress signal like this."""
        return {}

    # ---- Auto-ID seam ----
    # Used by create_case_in_folder() when case_id is omitted (or is
    # itself a CaseIDGenerator, overriding this for just that call). Override on a
    # subclass to share one generator across case types, run multiple namespaces, or
    # encode limited type info into the id. Default: short, sortable, base-36
    # millisecond time slug (in-process monotonic).
    case_id_generator: CaseIDGenerator = DEFAULT_CASE_ID_GENERATOR

    # Lease timing (TTL, beat throttle, in-flight pulse cadence) is a single FIXED policy in
    # constants.py (DEFAULT_LEASE_TTL_SECS et al.), deliberately not a per-case/per-state seam.
    # See "single-owner protection" in SECTION 4 for the crash-recovery-window rationale and
    # the idle-ownership contract.

    @_raises_when_detached
    def case_heartbeat(
        self,
        *,
        min_update_secs: float = LEASE_HEARTBEAT_THROTTLE_SECS,
        validate_ownership: bool = True,
    ) -> None:
        """Extend our lease. Raises `OwnershipLostError` when ownership is displaced.

        Quick use:
          The mainstream driver does NOT need to call this — case_advance() beats the lease
          for you. Call it directly only when you HOLD a bound case without advancing it (a
          custom dwell loop, or an idle holder choosing to keep the folder spoken-for rather
          than detaching — see the idle-ownership contract in SECTION 4)."""
        self._check_active()
        try:
            self._lease.heartbeat(
                min_update_secs=min_update_secs,
                validate_ownership=validate_ownership,
            )
        except LeaseOwnershipLostError as e:
            raise OwnershipLostError(self._folder) from e

    def trigger_warn_secs(self, trigger: str) -> float:
        """The SOFT timeout (seconds) for a trigger's work: its `~<dur>` DSL annotation if
        present, else DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS. The override seam for a per-case
        or dynamic budget — keep it cheap, it is consulted on every step that has work."""
        return self._fsm.trigger_timeouts.get(trigger, DEFAULT_TRIGGER_TIMEOUT_WARNING_SECS)


    def archive_grouping_label(self) -> str:
        """Destination archive grouping when this case closes. Default: close month
        (``YYYY-MM``). Override to key on creation date, fiscal period, tenant, etc."""
        return _utcnow().strftime("%Y-%m")

    async def case_run_blocking(self, fn, /, *args, **kwargs):
        """OPT-IN escape hatch for a SYNCHRONOUS/blocking call inside a `perform_`. Runs `fn`
        on the default thread-pool executor and awaits it, so the call does NOT freeze the
        event loop (which would stall every other case sharing it). Use ONLY when a library
        gives you no async API:

            async def perform_fetch(self, tctx):
                resp = await self.case_run_blocking(requests.get, url)

        SECOND-CLASS by design, with two caveats vs. an async-native client:
          * Concurrency is bounded by the executor's thread pool (not the ~unbounded
            concurrency of real async I/O), so blocking calls do not scale the same way.
          * The hard-abort (TriggerTimeout) cancels the AWAIT, but a running thread cannot be
            killed — the worker keeps going until `fn` returns on its own. So a true hang here
            frees the case but leaks the thread. Prefer an async client for anything that can
            hang."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    # ---- reclassify ("call an audible" to a different subclass) ----

    def case_reclassify_to(
        self, new_cls: type[FolderBackedCase]
    ) -> FolderBackedCase:
        """Rebind this case to a different FolderBackedCase subclass via a two-phase
        COMMIT, logging a CASE_RECLASSIFIED event. The CALLER owns compatibility.

        Advanced:
          Use when a case must change its TYPE mid-life (e.g. a generic intake becomes a
          specialized workflow) while keeping its folder, id, and history. The current
          state must be a valid state of `new_cls` or IncompatibleReclassError is raised.

        Maintainer notes:
          Two-phase commit (crash-atomic):
            Phase 1 — snapshot old repr in the OLD class's schema; detach (free the lease).
            Phase 2 — NEW class acquires the now-free lease, CONSCIOUSLY stamps its
                      own name, force-commits in its own schema.
          A crash between phases reopens cleanly as the OLD class (name not switched)."""
        if self.case_state not in new_cls._fsm.states:
            raise IncompatibleReclassError(self.case_state, new_cls.__name__)
        from_name = self._record.case_object_type
        self._flush_record(force=True)           # phase 1: snapshot old repr (old name)
        self.case_detach()                       # serialize-then-DETACH, then re-acquire
        # Bind without the type gate: the old name is still on disk until phase 2 stamps the
        # new one. __new__ + _bind_existing_case_dir bypasses __init__'s gated public path.
        fresh = new_cls.__new__(new_cls)
        fresh._bind_existing_case_dir(self._folder, check_type=False)
        fresh._journal.log_reclassified(
            new_cls.__name__, from_type=from_name, at_state=fresh.case_state
        )
        fresh._record.case_object_type = new_cls.__name__   # CONSCIOUS stamp
        fresh._record.asset_aliases = type(fresh)._resolve_asset_book().to_record()
        fresh._record.fsm_state_chains = list(type(fresh).fsm_state_chains)
        fresh._flush_record(force=True)                      # phase 2: commit new name + schema
        type(fresh)._seed_keep_rules(fresh._keep_manifest)
        return fresh

    # =======================================================================
    # SECTION 4 — Internal mechanics (maintainers)
    # -----------------------------------------------------------------------
    # Construction/binding, the FSM state-change and exception choke points,
    # record flush, and other private machinery. One-step advance orchestration
    # lives in `_CaseAdvancer` (see case_advance() / case_is_advanceable façades).
    # Read this to MAINTAIN the class; you should not need it to USE it.
    # =======================================================================

    # ---- FSM compilation trigger (parse-once-at-class-definition) ----

    # Compiled per-class FSM (private): populated in __init_subclass__ via compile_fsm()
    # (the overridable default lives in SECTION 3). Empty on the base until a subclass
    # supplies chains.
    _fsm: FsmChainSpec = FsmChainSpec.empty()

    # The base's RESERVED instance call-surface: names that back core machinery and must
    # never be shadowed by a subclass. The binding check (validate_object_compatibility,
    # passed these via _bind_existing_case_dir) fails fast if a subclass redefines one in
    # its body, so descendants are free to use short names everywhere else. Deliberately
    # excludes the override SEAMS (compile_fsm, case_id_generator, on_terminating, case_dwell_secs,
    # ...) — those are MEANT to be overridden — and the hook-name conventions
    # (perform_/before_/after_/on_enter_/on_exit_/guard_), which belong to the subclass.
    _SEALED_MEMBER_NAMES: frozenset[str] = frozenset({
        "case_state", "case_folder", "case_assets", "case_external_key",
        "case_is_live", "case_is_terminal", "case_terminal_states",
        "case_transition_fail_count", "case_id",
        "case_advance", "case_detach", "case_heartbeat", "case_record",
        "case_log_alert", "case_run_blocking", "case_reclassify_to",
    })

    # Public members deliberately absent from FolderBackedCaseInterface
    # (advanced / rare seams). The alignment audit requires every public name
    # on this class to be either on the interface or listed here.
    _advanced_members: frozenset[str] = frozenset({
        # define-time seams
        "case_type_spec",
        "compile_fsm",
        "case_id_generator",
        # overridable event handlers
        "on_transition_exception",
        "on_terminating",
        # extended-status hook (polled)
        "case_ext_status_info",
        # runtime seams & rare operations
        "case_heartbeat",
        "trigger_warn_secs",
        "archive_grouping_label",
        "case_run_blocking",
        "case_reclassify_to",
    })

    @staticmethod
    def _assert_interface_alignment() -> None:
        """CI-only audit (see tests/test_folder_backed_case_interface.py): every public
        name FolderBackedCase declares must be either on FolderBackedCaseInterface or in
        _advanced_members, never both, and neither list may go stale. Not called at
        import time — only a maintainer editing this repo can cause drift, and drift
        is documentation-only, never a behavior bug."""
        def is_public(name: str) -> bool:
            return not name.startswith("_")

        fb_public = {
            n for n in {**vars(FolderBackedCase), **getattr(FolderBackedCase, "__annotations__", {})}
            if is_public(n)
        }
        iface_public = {
            n for n in {
                **vars(FolderBackedCaseInterface),
                **getattr(FolderBackedCaseInterface, "__annotations__", {}),
            }
            if is_public(n)
        }
        advanced = FolderBackedCase._advanced_members

        missing = fb_public - iface_public - advanced
        double_listed = iface_public & advanced
        stale_advanced = advanced - fb_public
        stale_interface = {
            n for n in iface_public
            if not hasattr(FolderBackedCase, n) and n not in ("log",)
        }

        problems: list[str] = []
        if missing:
            problems.append(
                f"Public on FolderBackedCase but neither on the interface nor in "
                f"_advanced_members: {sorted(missing)}. Fix: declare each on "
                f"FolderBackedCaseInterface with a contract docstring, or add it to "
                f"FolderBackedCase._advanced_members."
            )
        if double_listed:
            problems.append(
                f"On both the interface and _advanced_members: {sorted(double_listed)}. "
                f"Fix: remove from one."
            )
        if stale_advanced:
            problems.append(
                f"_advanced_members names no longer public on FolderBackedCase: "
                f"{sorted(stale_advanced)}. Fix: remove from _advanced_members."
            )
        if stale_interface:
            problems.append(
                f"FolderBackedCaseInterface declares names not found on FolderBackedCase: "
                f"{sorted(stale_interface)}. Fix: remove the stale stub or restore the member."
            )
        if problems:
            raise AssertionError("\n".join(problems))

    @staticmethod
    def _copy_interface_docstrings() -> None:
        """Copy contract docstrings from FolderBackedCaseInterface onto this class's
        overrides that deliberately omit their own docstring. Python does not inherit
        method/property docs across overrides; this one-shot sync restores the
        approved "interface docs show up on FolderBackedCase members" behavior for
        help()/inspect.getdoc/IDE hover."""
        for name, iface_obj in vars(FolderBackedCaseInterface).items():
            if name.startswith("_"):
                continue
            if name not in vars(FolderBackedCase):
                continue
            fb_obj = vars(FolderBackedCase)[name]
            if isinstance(iface_obj, property) and isinstance(fb_obj, property):
                if not fb_obj.__doc__ and iface_obj.__doc__:
                    setattr(
                        FolderBackedCase,
                        name,
                        property(fb_obj.fget, fb_obj.fset, fb_obj.fdel, iface_obj.__doc__),
                    )
                continue
            if isinstance(iface_obj, (classmethod, staticmethod)) and type(fb_obj) is type(iface_obj):
                iface_fn = iface_obj.__func__
                fb_fn = fb_obj.__func__
                if not fb_fn.__doc__ and iface_fn.__doc__:
                    fb_fn.__doc__ = iface_fn.__doc__
                continue
            if callable(iface_obj) and callable(fb_obj):
                if not getattr(fb_obj, "__doc__", None) and getattr(iface_obj, "__doc__", None):
                    fb_obj.__doc__ = iface_obj.__doc__

    def __init_subclass__(cls, **kwargs) -> None:
        # Parse + validate at class-definition time: fail-fast (a malformed OR missing
        # declaration blows up at import, not first instantiation) and performant
        # (compiled once, not per instance). The result is the shared per-class FSM
        # singleton. Every declaration this class must supply (chains, chokes, asset
        # aliases) is checked here, uniformly, the moment the class statement finishes
        # executing — none of it waits for someone to try to instantiate the class.
        #
        # NOT checked here: hook-method completeness (validate_object_compatibility).
        # That stays at first instantiation (_bind_existing_case_dir) — deliberately,
        # see the comment there.
        super().__init_subclass__(**kwargs)
        cls._require_fsm_trigger_chokes_declared()
        cls._fsm = cls.compile_fsm()
        if not cls._fsm.states:
            raise MissingFsmError(cls.__name__)
        if cls.asset_aliases is None:
            raise MissingAssetSchemaError(cls.__name__)
        # allowed to be empty
        cls._asset_book = AliasedAssetSpecs.from_declaration(
            cls.asset_aliases, flexible=cls.flexible_asset_alias_loading,
        )
        if not cls._asset_book.aliases():
            logger.warning(
                "%r declares asset_aliases but the alias set is empty — no "
                "protocol-elevated data objects are registered for cross-process trust.",
                cls.__name__,
            )
        cls._asset_book.validate_against_fsm(cls._fsm, flexible=cls.flexible_asset_alias_loading)
        if cls._fsm.primary_chain is not None:
            logger.debug(
                "FSM for %s: chains compiled (primary=%r, initial=%r, initial_states=%s, "
                "terminal=%s, auto-advance=%s)",
                cls.__name__, cls._fsm.primary_chain, cls._fsm.initial_state,
                sorted(cls._fsm.initial_states), sorted(cls._fsm.terminal_states),
                cls._fsm.pipeline,
            )

    # ---- construction / binding ----

    def __init__(self, case_folder: Path):
        """Bind a live case object to an EXISTING on-disk case folder.

        This constructor is intentionally the load/bind path, not inception:
        it expects `case_record.yaml` (and any existing event-log history) to
        already exist on disk, then loads them, acquires the lease, and builds
        the in-memory FSM carrier. Call ``case_detach()`` when you are done with
        this live object. Two ways it fails fast and points elsewhere:
          * The folder is not an initialized case (no record) -> FileNotFoundError
            naming ``create_case_in_folder()`` and ``case_type_registry.rehydrate(folder)``.
          * The record names a DIFFERENT case type than this class -> the
            CaseTypeMismatchError gate.

        Brand-new cases are created via `create_case_in_folder(...)`, which first
        materializes the folder + record (minting `case_id` via
        `case_id_generator` when needed), then immediately calls this
        constructor to attach the live object.

        Retention at close is manifest-driven: ``_keep.txt`` at the case root lists
        every file that survives purge. Framework artifacts are seeded automatically;
        for assets, prefer declaring ``AssetSpec(keep=True)`` (seeded automatically
        too); for any other runtime keep decision, call ``case_keep_files()``."""
        self._bind_existing_case_dir(case_folder, check_type=True)

    def _bind_existing_case_dir(
        self, case_folder: Path, *, check_type: bool = True
    ) -> None:
        """Load an existing case folder and bind this live object to it.

        Shared by ``__init__`` (``check_type=True``) and ``case_reclassify_to``
        (``check_type=False`` during the two-phase type switch).
        """
        # Run config guards BEFORE any disk/lease I/O so misconfigured classes fail cleanly.
        cls = type(self)
        # 1) Every real subclass already had this checked in __init_subclass__ (class
        # definition time). This is dead code for subclasses; it only still matters if
        # someone directly instantiates FolderBackedCase itself, which never runs
        # __init_subclass__ and so never gets a compiled FSM.
        if not cls._fsm.states:
            raise MissingFsmError(cls.__name__)
        # 2) One-time carrier binding check, keyed on cls.__dict__ so subclasses don't
        # inherit a parent's "already checked" sentinel. Uses the method's default
        # orphan_detection="error": a hook/guard-looking method that maps to no known
        # state/trigger/guard is treated as a typo and fails the build.
        #
        # Kept here, not in __init_subclass__: intermediate bases may leave hooks for a
        # later leaf to supply; checking this at class-definition time would break that.
        if "_fsm_binding_checked" not in cls.__dict__:
            cls._fsm.validate_object_compatibility(
                self,
                sealed_names=FolderBackedCase._SEALED_MEMBER_NAMES,
                sealed_owner=FolderBackedCase,
            )
            cls._fsm_binding_checked = True
        self._folder = Path(case_folder)
        # Uninitialized-folder gate: missing record means create_case_in_folder() or
        # case_type_registry.rehydrate(folder) was intended instead.
        record_path = self._folder / RECORD_NAME
        if not record_path.exists():
            raise FileNotFoundError(
                f"No case record at {record_path}: this folder is not an initialized case. "
                "Use create_case_in_folder() to incept a new case, or "
                "case_type_registry.rehydrate(folder) to open an existing one by type."
            )
        self._lease: HeartbeatLease | None = None
        # without_lock=True: the case lease is our single-owner mechanism; the mixin's file
        # lock is redundant for reads and would block re-opens. save() still acquires its
        # own short-lived lock per write.
        self._record: CaseRecord = self._record_cls.open(
            str(record_path), without_lock=True
        )
        # Wrong-type gate (before the lease, so a reject claims nothing): this class is not
        # the one the record names. case_reclassify_to passes check_type=False to build over the
        # old name on purpose; every other caller gets the gate.
        if check_type and self._record.case_object_type != cls.__name__:
            raise CaseTypeMismatchError(
                on_disk=self._record.case_object_type, loading_class=cls.__name__
            )
        self._journal = CaseEventJournal.for_folder(self._folder)
        self._keep_manifest = CaseKeepManifest(self._folder)
        self._keep_manifest.ensure_framework_rules()
        self._assets = CaseAssets(
            self._folder,
            asset_specs=type(self)._resolve_asset_book().spec_map(),
            flexible_asset_alias_loading=cls.flexible_asset_alias_loading,
            keep_manifest=self._keep_manifest,
        )
        # State is derived from the event log on load (most recent CASE_STATE_ENTERED);
        # transitions then caches it on _case_state (the machine's model_attribute),
        # exposed read-only via the case_state property defined in SECTION 3 above.
        self._case_state: str = self._journal.current_state or self._fsm.initial_state
        # Event-log mtimes are LOCAL naive (datetime.fromtimestamp); _local_mtime_as_utc()
        # converts them to aware UTC. record.created is already aware UTC (CaseRecord
        # validator).
        self._last_activity_at: datetime.datetime = (
            _local_mtime_as_utc(self._journal.last_activity_at) or self._record.created
        )
        # When the CURRENT state was entered — dwell anchor for @DWELL guards,
        # from the latest CASE_STATE_ENTERED; a brand-new case has none yet, so fall back.
        self._state_entered_at: datetime.datetime = (
            _local_mtime_as_utc(self._journal.last_state_entered_mtime()) or self._record.created
        )
        # The lease TTL is a single fixed crash-recovery window (see constants.py); the
        # provider is a constant function, not a per-state policy.
        self._lease = HeartbeatLease(
            self._folder / LEASE_NAME,
            ttl_provider=lambda: DEFAULT_LEASE_TTL_SECS,
        )
        try:
            self._lease.acquire()
        except LeaseAlreadyHeldError as e:
            raise CaseAlreadyOpenError(self._folder, expires_in=e.expires_in) from e
        # Per-case folder-logging tee (always on). Set up only AFTER the lease is held, so we
        # never write into a folder owned by another process. The state provider is
        # weakref-based so the per-instance logger never pins its owning case, and the
        # close-after-write handler holds no persistent fd (thousands of live cases cost ~0
        # open descriptors at rest).
        _case_ref = weakref.ref(self)
        self.log = build_case_logger(
            self._record.case_id,
            self._folder / LOGS_DIR_NAME / LOG_FILE_NAME,
            case_object_type=type(self).__name__,
            state_provider=lambda: (c := _case_ref()) and c.case_state,
        )
        write_attach_banner(self.log)
        # Non-reentrancy guard (see _on_prepare_fsm_event / CaseTransitionInFlightError):
        # at most one FSM trigger invocation may be in flight on this object at a time.
        # Reset here so a freshly (re)bound instance — including case_reclassify_to's
        # fresh object — never inherits a stale flag.
        self._transition_in_flight: bool = False
        self._active_trigger_name: str | None = None
        # Process-lifetime sticky: last unrestricted case_advance() proved AutoAdvanceBlocked.
        # See case_was_blocked / _CaseAdvancer._apply_was_blocked.
        self._was_blocked: bool = False
        # True while case_advance() owns set/clear of _was_blocked (suppress direct-trigger clear).
        self._in_case_advance: bool = False
        # Instance-time machine binding is delegated to _CaseMachineFactory.
        self._machine = _CaseMachineFactory(self, self._fsm, self._journal).build(self.case_state)

    # ---- FSM attachment via transitions (async-first composition pattern) ----
    # Machine construction + callback wiring live in _CaseMachineFactory; the case keeps
    # the override seams the factory reads back (trigger_warn_secs, case_dwell_secs) and the
    # named lifecycle callbacks the machine binds (_on_state_changed, _on_fsm_exception,
    # _on_prepare_fsm_event, _on_finalize_fsm_event).

    def _on_prepare_fsm_event(self, event_data) -> None:
        """Machine `prepare_event` hook — the single chokepoint EVERY trigger call passes
        through before any guard/condition runs (covers case_advance() and a direct
        `await case.<trigger>()` alike). Enforces the non-reentrancy invariant: at most
        one FSM event may be in flight on this object at a time.

        DELIBERATELY SYNCHRONOUS (no `await` before the check-and-set): the `transitions`
        library runs this to completion without yielding the event loop, so the
        check-and-set below is atomic with respect to any OTHER concurrent trigger call
        on this same object — whichever call's prepare hook actually executes first
        always wins, regardless of scheduling order. See CaseTransitionInFlightError.

        Marks `event_data` (not just `self`) as "mine to clear" via `_case_guard_owner`:
        `finalize_event` below ALWAYS fires exactly once per trigger() call — even for a
        call rejected before prepare_event ever ran (e.g. the trigger has no edge from
        the CURRENT state) — so finalize must only clear the flag for the call that
        actually set it, or it could wrongly release a flag held by a genuinely in-flight
        transition."""
        if self._transition_in_flight:
            raise CaseTransitionInFlightError(
                self.case_id, self._folder, self._active_trigger_name
            )
        self._transition_in_flight = True
        self._active_trigger_name = (
            event_data.event.name if event_data.event is not None else None
        )
        event_data._case_guard_owner = True
        # Direct trigger calls clear the sticky blocked observation; case_advance()
        # owns set/clear itself while _in_case_advance is True.
        if not self._in_case_advance:
            self._was_blocked = False

    def _on_finalize_fsm_event(self, event_data) -> None:
        """Pairs with `_on_prepare_fsm_event` — see that method for why the ownership
        check (rather than an unconditional clear) matters."""
        if getattr(event_data, "_case_guard_owner", False):
            self._transition_in_flight = False
            self._active_trigger_name = None

    def _on_state_changed(self, event) -> None:
        """Runs after EVERY transition (event is a transitions EventData). Records the
        state-change entry, then on non-terminating transitions throttled-flushes the record
        and beats the lease. On the non-terminal → terminal EDGE, runs the two-phase termination.

        Two-phase termination:
          Phase 1 — PRE-FINALIZATION (assets still exist):
            1. Log CASE_TERMINATED event
            2. on_terminating() — subclass retains/extracts final artifacts
          Phase 2 — POST-FINALIZATION (immutable, still BOUND):
            3. _keep_manifest.purge() — drop everything not matched in _keep.txt
            4. _record.terminal + _record.terminal_state stamped + FORCE-flushed
               (authoritative seal)
            5. heartbeat(force) — keep the lock fresh; termination does NOT detach (the
               object stays bound so owners can harvest before calling case_detach());
               the "safe to move" signal is case_detach(). External observers get
               TERMINATED from the pool driver's event framework, or the journal.
        """
        src, dest = event.transition.source, event.transition.dest
        trigger = event.event.name if event.event is not None else None
        self._journal.log_state_entered(dest, trigger=trigger, from_state=src)
        self._last_activity_at = _utcnow()
        self._state_entered_at = self._last_activity_at   # reset the time-guard dwell anchor
        terminating = src not in self._fsm.terminal_states and dest in self._fsm.terminal_states
        if not terminating:
            # Throttled flush + lease beat at the boundary. Skipped on the terminating edge:
            # the forced phase-2 seal supersedes the flush, and phase 2 beats explicitly.
            self._flush_record()
            self.case_heartbeat()
        else:
            # --- phase 1: pre-finalization --- assets still present ---
            self._journal.log_terminated(dest, from_state=src)
            self.on_terminating()
            # --- phase 2: post-finalization --- assets gone, record sealed ---
            # ONE purge process for everything ephemeral: unmatched keepfile paths
            # are deleted (logs included unless a keep rule covers them).
            self._keep_manifest.purge()
            self._record.terminal = self._last_activity_at
            self._record.terminal_state = dest
            self._flush_record(force=True)
            # Termination keeps the lock; it does NOT detach. Force a fresh beat so the now-idle
            # (un-advanced) terminal case holds a full-TTL grace window for owners to harvest
            # before they call case_detach(). A crash still lapses the lock via the TTL.
            self.case_heartbeat(min_update_secs=0)

    async def _on_fsm_exception(self, event) -> None:
        """Machine-level `on_exception` hook (wired by the machine factory): the SINGLE chokepoint
        every trigger dispatch funnels through, so it covers both case_advance() and a direct
        `await case.<trigger>()`. Fires when ANY callback raises — a guard, a
        `perform_<trigger>`, on_exit/on_enter, or an `after`.

        It distinguishes the COMMIT BOUNDARY without needing to know which callback slot
        raised: `transitions` writes the dest to `_case_state` during the state change,
        BEFORE on_enter/after run, so `self.case_state == dest` means we are POST-commit.

        Steps, in order:
          1. NO-DRIFT REMEDY (post-commit only): if we advanced in memory but the durable
             CASE_STATE_ENTERED write never ran (the after_state_change writer was skipped by
             the raise), write it now so the on-disk log can never lag in-memory state.
          2. DECORATE the exception with structured `case_context` (case_id, trigger,
             source/dest, commit phase) so a type-agnostic driver can branch without parsing
             messages — attached HERE so it travels regardless of how the trigger was fired.
          3. LOG a terse, COUNTABLE failure fact (NOT a CASE_ALERTED): CASE_TRANSITION_FAILED for
             a pre-commit failure (the kind `@FAIL` counts and retries re-attempt), or
             CASE_ENTRY_EXCEPTION for a post-commit entry-hook raise (it DID enter; logged
             and hooked, but NOT counted by @FAIL). A pre-commit TriggerTimeout is logged as
             CASE_TRIGGER_TIMED_OUT instead — visually distinct, still @FAIL-counted.
          4. Call on_transition_exception(...) so the case may compensate.
          5. RE-RAISE the original exception. case_advance() catches it and folds it into an
             AdvanceResult; a direct caller gets the raise (fail-fast preserved)."""
        err = event.error
        # Lost the folder to another owner mid-step (raised by the keepalive pulse): a fatal
        # invariant breach, not a transition failure. Surface it WITHOUT logging a fail or
        # counting it toward @FAIL — the displaced owner must simply stop operating.
        if isinstance(err, OwnershipLostError):
            raise err
        # Reentrancy rejection from _on_prepare_fsm_event: raised BEFORE any guard/condition
        # ran for THIS call, so there is nothing here to decorate or log — no transition was
        # even attempted. Surface it exactly like OwnershipLostError: a misuse/invariant
        # signal, not a transition outcome (never folded into AdvanceResult; see
        # CaseTransitionInFlightError and `_CaseAdvancer._attempt_one_trigger`'s matching re-raise).
        if isinstance(err, CaseTransitionInFlightError):
            raise err
        trigger = event.event.name if event.event is not None else None
        trans = event.transition
        src = trans.source if trans is not None else self.case_state
        dest = trans.dest if trans is not None else None
        post_commit = dest is not None and self.case_state == dest

        # 1. No-drift remedy. NOTE (sharp edge): if dest is a TERMINAL state, the two-phase
        # termination in _on_state_changed was also skipped here; we reconcile the
        # CASE_STATE_ENTERED but do NOT attempt termination from inside the exception
        # handler. Terminal-entry failures still need the termination path made
        # idempotent/re-runnable.
        if post_commit and self._journal.current_state != dest:
            self._journal.log_state_entered(dest, trigger=trigger, from_state=src)
            self._last_activity_at = _utcnow()
            self._state_entered_at = self._last_activity_at

        # 2. Decorate.
        context = {
            "case_id": self.case_id,
            "trigger": trigger,
            "source": src,
            "dest": dest,
            "phase": "post_commit" if post_commit else "pre_commit",
        }
        try:
            err.case_context = context        # best-effort; some exceptions forbid attrs
        except (AttributeError, TypeError):
            pass

        # 3. Log the countable failure fact. A TriggerTimeout is pre-commit by construction
        # (the work runs in `before`), but gets its OWN distinct label so a timeout never
        # reads like an ordinary transition failure — while count_fails_this_dwell still
        # counts it.
        detail = {"trigger": trigger, "source": src, "dest": dest,
                  "error": type(err).__name__, "msg": str(err)[:200]}
        if isinstance(err, TriggerTimeout):
            self._journal.log_trigger_timed_out(trigger or src, detail)
        elif post_commit:
            self._journal.log_entry_exception(dest or src, detail)
        else:
            self._journal.log_transition_failed(trigger or src, detail)

        # 3b. Tee the full traceback into the case's own log (and, via propagation, the
        # main log) — the journal fact above is terse-by-design (type name + truncated
        # message), so this is the ONLY place a subclass's own trigger/guard/hook
        # exceptions get a full stack trace in logs/case.log. Safe to reach this far down:
        # both early returns above (OwnershipLostError, CaseTransitionInFlightError) raise
        # BEFORE this point, so we never log — or otherwise touch the folder — on behalf
        # of a call that no longer legitimately owns it.
        self.log.error(
            "trigger %r raised %s during transition %s -> %s",
            trigger, type(err).__name__, src, dest, exc_info=err,
        )

        # 4. Let the case react. final_state reflects where we actually ended up.
        final_state = dest if post_commit else src
        try:
            self.on_transition_exception(src, trigger, final_state, err)
        except Exception:                     # a misbehaving hook must not mask the real error
            self.log.exception("on_transition_exception hook raised for case %s", self.case_id)

        # 5. Re-raise so case_advance() can fold it in (and direct callers stay fail-fast).
        raise err

    # ---- record flush protocol (single guarded chokepoint) ----
    # Reads use without_lock=True (open() would otherwise hold a lock for the
    # instance lifetime, which conflicts with our single-owner lease model).
    # Writes use the mixin's save() as normal — it acquires a brief transient
    # lock only for the duration of the write and releases immediately after.
    # The public read companion, case_record(), lives in SECTION 2.

    def _flush_record(self, *, force: bool = False) -> None:
        """Persist the owned record. Default is THROTTLED — writes only when
        the in-memory record differs from its last-saved snapshot; force=True
        writes unconditionally (the authoritative seal at create/close/reclassify).
        A class may only ever write its OWN name: case_object_type must equal
        type(self).__name__ or RecordTypeMismatchError is raised.
        ALL live-instance record writes MUST funnel through here for the guard."""
        expected = type(self).__name__
        if self._record.case_object_type != expected:
            raise RecordTypeMismatchError(self._record.case_object_type, expected)
        if force or self._record.is_modified():
            self._record.save()

    # Lease TTL is a fixed crash-recovery window (constants.py). Idle holders must
    # detach, heartbeat, or delegate to a planned CaseManager (draft: notebooks/DEVDAVE/
    # case_manager_classes/CaseManager Model.md) for fleet-wide keepalive.

    def _check_active(self) -> None:
        if self.case_is_detached:
            raise DetachedCaseError(self._folder)

    def __del__(self):
        try:
            self.case_detach()
        except Exception:
            pass

    # ---- stall handling (no self-pulse by design: see `_CaseAdvancer.advance` and
    # notebooks/DEVDAVE/case_manager_classes/_backlog/finishing_watchdog.md) ----


# One-shot: attach interface contract docs to overrides that left __doc__ empty.
FolderBackedCase._copy_interface_docstrings()
