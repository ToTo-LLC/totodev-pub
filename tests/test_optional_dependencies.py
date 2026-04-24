from __future__ import annotations

import pytest

from totodev_pub.optional_dependencies import (
    build_missing_dependency_message,
    raise_missing_dependency,
)


def test_build_missing_dependency_message_includes_extra_hint() -> None:
    message = build_missing_dependency_message(
        feature="Gmail support",
        packages=["google-auth", "google-api-python-client"],
        extra="connectors",
    )

    assert "Gmail support requires optional dependencies: google-auth, google-api-python-client." in message
    assert 'Install with: pip install "totodev-pub[connectors]"' in message
    assert "Or install packages directly: pip install google-auth google-api-python-client" in message


def test_build_missing_dependency_message_without_extra_hint() -> None:
    message = build_missing_dependency_message(
        feature="TOML support",
        packages=["tomli-w"],
    )

    assert "TOML support requires optional dependencies: tomli-w." in message
    assert 'totodev-pub[' not in message
    assert "Install packages directly: pip install tomli-w" in message


def test_raise_missing_dependency_raises_importerror() -> None:
    with pytest.raises(ImportError) as exc_info:
        raise_missing_dependency(
            feature="SSH support",
            packages=["paramiko"],
            extra="connectors",
        )

    assert "SSH support requires optional dependencies: paramiko." in str(exc_info.value)
