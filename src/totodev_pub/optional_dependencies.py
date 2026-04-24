"""Helpers for consistent optional dependency guidance."""

from __future__ import annotations

from typing import Iterable


def build_missing_dependency_message(
    *,
    feature: str,
    packages: Iterable[str],
    extra: str | None = None,
) -> str:
    seen: set[str] = set()
    package_list: list[str] = []
    for package in packages:
        normalized = package.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        package_list.append(normalized)
    package_csv = ", ".join(package_list)
    package_cli = " ".join(package_list)

    if not package_list:
        raise ValueError("packages must contain at least one non-empty dependency name")

    message = f"{feature} requires optional dependencies: {package_csv}."
    if extra:
        message += f' Install with: pip install "totodev-pub[{extra}]".'
        message += f" Or install packages directly: pip install {package_cli}"
    else:
        message += f" Install packages directly: pip install {package_cli}"
    return message


def raise_missing_dependency(
    *,
    feature: str,
    packages: Iterable[str],
    extra: str | None = None,
) -> None:
    raise ImportError(
        build_missing_dependency_message(
            feature=feature,
            packages=packages,
            extra=extra,
        )
    )
