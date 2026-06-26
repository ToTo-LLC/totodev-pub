import pytest

from totodev_pub.folder_backed_case_support.exceptions import FsmBindingError
from totodev_pub.folder_backed_case_support.exceptions import FsmChainParseError
from totodev_pub.folder_backed_case_support.state_chain_parser import StateChainParser


def test_auto_edge_uses_double_equals_connector():
    spec = StateChainParser.parse(["^new==begin-->open--finish-->done^"])

    assert ("new", "begin") in spec.auto_edges
    assert spec.pipeline == ["begin"]
    assert spec.transitions[0]["trigger"] == "begin"
    assert spec.transitions[0]["source"] == "new"
    assert spec.transitions[0]["dest"] == "open"


def test_legacy_star_auto_edge_syntax_is_rejected():
    with pytest.raises(FsmChainParseError):
        StateChainParser.parse(["^new--*begin-->open--finish-->done^"])


def test_missing_hook_for_auto_trigger_is_rejected():
    spec = StateChainParser.parse(["^new==assign-->assigned^"])

    class Carrier:
        pass

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "_perform_assign" in msg
    assert "auto-advance" in msg


def test_missing_hook_for_manual_trigger_is_allowed():
    spec = StateChainParser.parse(["^new--assign-->assigned^"])

    class Carrier:
        pass

    spec.validate_object_compatibility(Carrier())


def test_required_auto_hook_must_be_async():
    spec = StateChainParser.parse(["^new==assign-->assigned^"])

    class Carrier:
        def _perform_assign(self, event):
            return None

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier())
    msg = str(excinfo.value)
    assert "_perform_assign" in msg
    assert "synchronous" in msg


def test_implied_carrier_attributes_require_auto_hooks_only():
    spec = StateChainParser.parse(
        ["^new==assign-->assigned--notify-->done^"]
    )

    required, optional = spec.implied_carrier_attributes()
    assert "_perform_assign" in required
    assert "_perform_notify" in optional


def test_hyphenated_state_name_is_rejected():
    with pytest.raises(FsmChainParseError):
        StateChainParser.parse(["^new-item--begin-->open^"])


def test_orphan_detection_error_mode_rejects_unknown_hook_methods():
    spec = StateChainParser.parse(["^new==assign-->assigned^"])

    class Carrier:
        async def _perform_assign(self, event):
            return None

        async def on_enter_assgined(self, event):  # typo: assigned
            return None

    with pytest.raises(FsmBindingError) as excinfo:
        spec.validate_object_compatibility(Carrier(), orphan_detection="error")
    msg = str(excinfo.value)
    assert "orphan" in msg
    assert "on_enter_assgined" in msg


def test_orphan_detection_warn_mode_emits_warning():
    spec = StateChainParser.parse(["^new==assign-->assigned^"])

    class Carrier:
        async def _perform_assign(self, event):
            return None

        async def before_assgin(self, event):  # typo: assign
            return None

    with pytest.warns(UserWarning):
        spec.validate_object_compatibility(Carrier(), orphan_detection="warn")


def test_orphan_detection_off_mode_suppresses_orphan_checks():
    spec = StateChainParser.parse(["^new==assign-->assigned^"])

    class Carrier:
        async def _perform_assign(self, event):
            return None

        async def after_assgin(self, event):  # typo: assign
            return None

    spec.validate_object_compatibility(Carrier(), orphan_detection="off")


def test_orphan_detection_requires_valid_mode():
    spec = StateChainParser.parse(["^new==assign-->assigned^"])

    class Carrier:
        async def _perform_assign(self, event):
            return None

    with pytest.raises(ValueError):
        spec.validate_object_compatibility(Carrier(), orphan_detection="maybe")
