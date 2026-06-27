import pytest

from totodev_pub.folder_backed_case_support.exceptions import FsmBindingError
from totodev_pub.folder_backed_case_support.exceptions import FsmChainParseError
from totodev_pub.folder_backed_case_support.state_chain_parser import StateChainParser


def test_auto_edge_uses_double_dash_connector():
    spec = StateChainParser.parse(["^new--begin-->open==finish-->done^"])

    assert ("new", "begin") in spec.auto_edges
    assert spec.pipeline == ["begin"]
    assert spec.transitions[0]["trigger"] == "begin"
    assert spec.transitions[0]["source"] == "new"
    assert spec.transitions[0]["dest"] == "open"


def test_legacy_star_auto_edge_syntax_is_rejected():
    with pytest.raises(FsmChainParseError):
        StateChainParser.parse(["^new--*begin-->open--finish-->done^"])


def test_missing_hook_for_auto_trigger_is_rejected():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        pass

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "perform_assign" in msg
    assert "auto-advance" in msg


def test_missing_hook_for_manual_trigger_is_allowed():
    spec = StateChainParser.parse(["^new==assign-->assigned^"])

    class Carrier:
        pass

    spec.validate_object_compatibility(Carrier())


def test_required_auto_hook_must_be_async():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        def perform_assign(self, event):
            return None

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "perform_assign" in msg
    assert "synchronous" in msg


def test_implied_carrier_attributes_require_auto_hooks_only():
    spec = StateChainParser.parse(
        ["^new--assign-->assigned==notify-->done^"]
    )

    required, optional = spec.implied_carrier_attributes()
    assert "perform_assign" in required
    assert "perform_notify" in optional


def test_hyphenated_state_name_is_rejected():
    with pytest.raises(FsmChainParseError):
        StateChainParser.parse(["^new-item--begin-->open^"])


def test_orphan_detection_error_mode_rejects_unknown_hook_methods():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self, event):
            return None

        async def on_enter_assgined(self, event):  # typo: assigned
            return None

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier(), orphan_detection="error")
    msg = str(excinfo.value)
    assert "orphan" in msg
    assert "on_enter_assgined" in msg


def test_orphan_detection_warn_mode_emits_warning():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self, event):
            return None

        async def before_assgin(self, event):  # typo: assign
            return None

    with pytest.warns(UserWarning):
        spec.validate_object_compatibility(Carrier(), orphan_detection="warn")


def test_orphan_detection_off_mode_suppresses_orphan_checks():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self, event):
            return None

        async def after_assgin(self, event):  # typo: assign
            return None

    spec.validate_object_compatibility(Carrier(), orphan_detection="off")


def test_orphan_detection_requires_valid_mode():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        async def perform_assign(self, event):
            return None

    with pytest.raises(ValueError):
        spec.validate_object_compatibility(Carrier(), orphan_detection="maybe")


# ---------------------------------------------------------------------------
# Method-guard `guard_<token>` convention
# ---------------------------------------------------------------------------

def test_method_guard_token_maps_to_guard_prefix():
    spec = StateChainParser.parse(["^new==funded#approved#finish-->done^"])

    conds = spec.transitions[-1]["conditions"]
    assert conds == ["guard_funded", "guard_approved"]


def test_missing_guard_method_is_rejected_with_prefixed_name():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def funded(self, event):  # bare name: not the guard_ convention
            return True

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "guard_funded" in msg


def test_async_guard_method_binds_cleanly():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def guard_funded(self, event):
            return True

    spec.validate_object_compatibility(Carrier())


def test_sync_guard_method_is_rejected():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        def guard_funded(self, event):  # sync: must be async
            return True

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "guard_funded" in msg
    assert "synchronous" in msg


def test_orphan_detection_error_mode_rejects_unknown_guard_method():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def guard_funded(self, event):
            return True

        async def guard_fundded(self, event):  # typo: funded
            return True

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier(), orphan_detection="error")
    msg = str(excinfo.value)
    assert "orphan" in msg
    assert "guard_fundded" in msg
    assert "guard" in msg


def test_orphan_detection_warn_mode_flags_unknown_guard_method():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def guard_funded(self, event):
            return True

        async def guard_fundded(self, event):  # typo: funded
            return True

    with pytest.warns(UserWarning):
        spec.validate_object_compatibility(Carrier(), orphan_detection="warn")


def test_orphan_detection_off_mode_allows_unknown_guard_method():
    spec = StateChainParser.parse(["^new==funded#finish-->done^"])

    class Carrier:
        async def guard_funded(self, event):
            return True

        async def guard_fundded(self, event):  # typo: funded
            return True

    spec.validate_object_compatibility(Carrier(), orphan_detection="off")


def test_factual_guards_do_not_imply_guard_methods():
    """`@DWELL`/`@FAIL` are compiled by the base class; they live in `_fact_guards`, not
    `conditions`, so they impose no `guard_<name>` carrier method and contribute nothing
    to the declared-guard set used by orphan detection."""
    spec = StateChainParser.parse(["^new--@DWELL>30m#timeout-->done^"])

    assert spec._declared_guard_tokens() == set()
    assert "conditions" not in spec.transitions[-1]

    class Carrier:
        async def perform_timeout(self, event):  # timeout is auto (`--`)
            return None

    spec.validate_object_compatibility(Carrier())
