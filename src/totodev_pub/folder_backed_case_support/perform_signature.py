# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""``perform_<trigger>`` signature as the trigger-kwargs contract.

The transitions library accepts unconstrained ``**kwargs`` on auto-generated
trigger methods and stuffs them into ``tctx.kwargs``. Authors declare the real
contract on ``perform_<trigger>``:

    async def perform_x(self, tctx, *, path: str, force: bool = False) -> None: ...

Define-time validation enforces that shape. At fire time the perform wrapper
binds ``tctx.kwargs`` to the keyword-only parameters, type-checks lightly, and
calls ``perform_*(tctx, **bound)``. Triggers with no ``perform_*`` are unchecked.
"""

from __future__ import annotations

import copy
import inspect
import types
from dataclasses import dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints

# Defaults we accept and deepcopy when applying (shared mutable default footgun).
_COPYABLE_MUTABLE_DEFAULT_TYPES = (list, dict, set)


def _params_after_self(fn) -> list[inspect.Parameter]:
    """Return parameters after an implicit/explicit ``self``, whether ``fn`` is
    bound or unbound. Bound methods omit ``self`` from ``inspect.signature``."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if inspect.ismethod(fn):
        return params
    if params and params[0].name == "self":
        return params[1:]
    return params


def _is_copyable_mutable_default(value: Any) -> bool:
    return isinstance(value, _COPYABLE_MUTABLE_DEFAULT_TYPES)


def _is_forbidden_mutable_default(value: Any) -> bool:
    """True for defaults that are mutable but not in our deepcopy allow-list."""
    if value is inspect.Parameter.empty:
        return False
    if _is_copyable_mutable_default(value):
        return False
    # Immutables / plain scalars are fine.
    try:
        hash(value)
        return False
    except TypeError:
        return True


def validate_perform_signature(fn) -> list[str]:
    """Define-time checks for a ``perform_<trigger>`` callable.

    Returns a list of human-readable problem strings (empty = OK). Does not raise.
    """
    problems: list[str] = []
    try:
        params = _params_after_self(fn)
    except (TypeError, ValueError) as exc:
        return [f"signature is not introspectable: {exc}"]

    if not params:
        problems.append(
            "must accept the trigger context as its first parameter after self "
            "(conventionally `tctx`)"
        )
        return problems

    tctx_param = params[0]
    if tctx_param.kind == inspect.Parameter.KEYWORD_ONLY:
        problems.append(
            f"first parameter after self ({tctx_param.name!r}) must be "
            "positional-or-keyword (conventionally `tctx`), not keyword-only"
        )
    elif tctx_param.kind not in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        problems.append(
            f"first parameter after self ({tctx_param.name!r}) must be "
            "positional-or-keyword (conventionally `tctx`)"
        )

    for param in params[1:]:
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            problems.append("must not declare *args")
            continue
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            problems.append("must not declare **kwargs")
            continue
        if param.kind != inspect.Parameter.KEYWORD_ONLY:
            problems.append(
                f"parameter {param.name!r} must be keyword-only "
                f"(declare it after `*`): got kind {param.kind.name}"
            )
            continue
        if param.annotation is inspect.Parameter.empty:
            problems.append(
                f"keyword-only parameter {param.name!r} requires a type annotation"
            )
        if _is_forbidden_mutable_default(param.default):
            problems.append(
                f"keyword-only parameter {param.name!r} has a mutable default "
                f"of type {type(param.default).__name__}; use list/dict/set "
                "(deepcopied on apply) or an immutable default"
            )

    return problems


def _resolved_annotations(fn) -> dict[str, Any]:
    """Resolve postponed annotations when possible; fall back to raw signature."""
    try:
        return get_type_hints(fn)
    except Exception:
        return {}


def _annotation_for(fn, param: inspect.Parameter, hints: dict[str, Any]) -> Any:
    if param.name in hints:
        return hints[param.name]
    return param.annotation


def _annotation_allows(value: Any, annotation: Any) -> bool:
    """Light runtime type check. ``Any`` always passes; unions try each arm;
    parameterized generics check the origin only (e.g. ``list[str]`` → ``list``)."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return True
    # Postponed annotations that were not resolved.
    if isinstance(annotation, str):
        if annotation in ("Any", "typing.Any"):
            return True
        # Best-effort: cannot check unresolved string forms lightly.
        return True

    origin = get_origin(annotation)
    if origin is None:
        if isinstance(annotation, type):
            return isinstance(value, annotation)
        return True

    # Union / Optional / X | Y
    if origin is Union or origin is types.UnionType:
        return any(_annotation_allows(value, arg) for arg in get_args(annotation))

    # Parameterized generics: list[str], dict[str, int], ...
    if isinstance(origin, type):
        return isinstance(value, origin)

    return True


def bind_perform_kwargs(fn, kwargs: dict) -> dict:
    """Bind ``tctx.kwargs`` to the keyword-only params of ``fn``.

    Returns the bound dict ready for ``fn(tctx, **bound)``. Raises
    :class:`~totodev_pub.folder_backed_case_support.exceptions.PerformParamsError`
    on missing/extra keys or type mismatches.
    """
    # Local import avoids a circular import at module load (exceptions is light,
    # but perform_signature is imported from the factory which already imports exceptions).
    from totodev_pub.folder_backed_case_support.exceptions import PerformParamsError

    try:
        params = _params_after_self(fn)
    except (TypeError, ValueError) as exc:
        raise PerformParamsError(
            f"perform method signature is not introspectable: {exc}"
        ) from exc

    if not params:
        raise PerformParamsError(
            "perform method has no trigger-context parameter after self"
        )

    kw_params = params[1:]  # skip tctx
    expected = {p.name: p for p in kw_params}
    incoming = dict(kwargs)

    unexpected = sorted(set(incoming) - set(expected))
    if unexpected:
        raise PerformParamsError(
            f"unexpected trigger kwargs: {unexpected}; "
            f"allowed: {sorted(expected) or '(none)'}"
        )

    bound: dict[str, Any] = {}
    missing: list[str] = []
    for name, param in expected.items():
        if name in incoming:
            bound[name] = incoming[name]
        elif param.default is not inspect.Parameter.empty:
            default = param.default
            if _is_copyable_mutable_default(default):
                bound[name] = copy.deepcopy(default)
            else:
                bound[name] = default
        else:
            missing.append(name)

    if missing:
        raise PerformParamsError(
            f"missing required trigger kwargs: {missing}"
        )

    type_errors: list[str] = []
    hints = _resolved_annotations(fn)
    for name, param in expected.items():
        value = bound[name]
        annotation = _annotation_for(fn, param, hints)
        if not _annotation_allows(value, annotation):
            type_errors.append(
                f"{name}={value!r} (expected {annotation!r}, "
                f"got {type(value).__name__})"
            )
    if type_errors:
        raise PerformParamsError(
            "trigger kwargs failed type checks: " + "; ".join(type_errors)
        )

    return bound


def _format_annotation(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return "?"
    if isinstance(annotation, type):
        return annotation.__name__
    text = str(annotation).replace("typing.", "")
    # Common cleanups from get_type_hints / builtins.
    text = text.replace("<class '", "").replace("'>", "")
    return text


@dataclass(frozen=True)
class PerformParam:
    """One keyword-only parameter of a ``perform_<trigger>`` method.

    The structured form behind :func:`format_perform_params`: unlike the
    display string, it carries the *raw* ``default`` value (not its ``repr``)
    and an explicit ``required`` flag, so callers that need to build real
    keyword arguments — e.g. a notebook generator emitting
    ``await wb.trigger("x", path=..., force=False)`` — have what they need
    without re-parsing the signature.
    """

    name: str
    annotation_display: str
    has_default: bool
    default: Any
    required: bool


def describe_perform_params(fn) -> list[PerformParam]:
    """Structured keyword-only params of a ``perform_<trigger>`` callable.

    Empty list when the signature is not introspectable. Skips ``tctx`` and any
    non-keyword-only parameter, mirroring :func:`format_perform_params` (which
    is now built on top of this).
    """
    try:
        params = _params_after_self(fn)
    except (TypeError, ValueError):
        return []
    hints = _resolved_annotations(fn)
    out: list[PerformParam] = []
    for param in params[1:]:
        if param.kind != inspect.Parameter.KEYWORD_ONLY:
            continue
        ann = _format_annotation(_annotation_for(fn, param, hints))
        has_default = param.default is not inspect.Parameter.empty
        out.append(
            PerformParam(
                name=param.name,
                annotation_display=ann,
                has_default=has_default,
                default=param.default if has_default else None,
                required=not has_default,
            )
        )
    return out


def format_perform_param(p: PerformParam) -> str:
    """Render one :class:`PerformParam` as ``path: str`` / ``force: bool = False``."""
    if p.required:
        return f"{p.name}: {p.annotation_display}"
    return f"{p.name}: {p.annotation_display} = {p.default!r}"


def format_perform_params(fn) -> list[str]:
    """Human-readable keyword-only param summaries for briefing/docs.

    Each entry looks like ``path: str`` or ``force: bool = False``.
    """
    return [format_perform_param(p) for p in describe_perform_params(fn)]
