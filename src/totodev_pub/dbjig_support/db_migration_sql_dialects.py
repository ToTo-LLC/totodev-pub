# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
SQL dialect generators for DbMigrationFileReader.

Provides dialect-specific generation of transaction boundaries (BEGIN/COMMIT),
identifier quoting, and VALUES clause formatting for INSERT statements.
All dialect classes use static methods only; no state.
"""

import re
from abc import ABCMeta, abstractmethod
from typing import Any, Dict, Sequence, Type


class _SQLDialectGenerator(metaclass=ABCMeta):
    """
    Abstract base for SQL dialect-specific snippet generation.

    Used by DbMigrationFileReader to generate BEGIN/COMMIT and INSERT VALUES
    in a dialect-appropriate form. User .sql file content is never rewritten.
    """

    @staticmethod
    @abstractmethod
    def dialect() -> str:
        """Return the dialect identifier (e.g. 'ansi', 'postgresql')."""
        ...

    @staticmethod
    @abstractmethod
    def begin_transaction() -> str:
        """Return the statement to start a transaction."""
        ...

    @staticmethod
    @abstractmethod
    def end_transaction() -> str:
        """Return the statement to commit the current transaction."""
        ...

    @staticmethod
    @abstractmethod
    def quote_identifier(name: str) -> str:
        """Return the identifier quoted for the dialect (or as-is if unquoted)."""
        ...

    @staticmethod
    @abstractmethod
    def insert(row_values: Sequence[Any]) -> str:
        """Return the full VALUES (v1, v2, ...) clause for one row."""
        ...

    @staticmethod
    def _looks_like_sql_expression(s: str) -> bool:
        """
        Return True if the string looks like a single SQL function/expression call.

        Pattern: starts with [A-Z_][A-Z0-9_]*, then '(', then any content, ends with ')'.
        Used to allow type coercion in data files (e.g. DATE('2023-07-15')) by
        passing such values through unquoted. No balanced-parentheses check.
        """
        if not s or not isinstance(s, str):
            return False
        t = s.strip()
        return bool(re.match(r"^[A-Z_][A-Z0-9_]*\(.*\)$", t))


def _escape_sql_string(value: str) -> str:
    """Escape single quotes by doubling (standard SQL)."""
    return value.replace("'", "''")


def _literal_value(value: Any, use_boolean_keywords: bool) -> str:
    """Format a single value as SQL literal. use_boolean_keywords: TRUE/FALSE vs 1/0."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        if use_boolean_keywords:
            return "TRUE" if value else "FALSE"
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if _SQLDialectGenerator._looks_like_sql_expression(value):
            return value
        return f"'{_escape_sql_string(value)}'"
    escaped = _escape_sql_string(str(value))
    return f"'{escaped}'"


class AnsiSqlDialect(_SQLDialectGenerator):
    """Generic ANSI-like dialect: BEGIN TRANSACTION, COMMIT, unquoted identifiers, 1/0 booleans."""

    @staticmethod
    def dialect() -> str:
        return "ansi"

    @staticmethod
    def begin_transaction() -> str:
        return "BEGIN TRANSACTION"

    @staticmethod
    def end_transaction() -> str:
        return "COMMIT"

    @staticmethod
    def quote_identifier(name: str) -> str:
        return name

    @staticmethod
    def insert(row_values: Sequence[Any]) -> str:
        literals = [_literal_value(v, use_boolean_keywords=False) for v in row_values]
        return "VALUES (" + ", ".join(literals) + ")"


class PostgreSQLDialect(_SQLDialectGenerator):
    """PostgreSQL: START TRANSACTION, COMMIT, double-quoted identifiers, TRUE/FALSE."""

    @staticmethod
    def dialect() -> str:
        return "postgresql"

    @staticmethod
    def begin_transaction() -> str:
        return "START TRANSACTION"

    @staticmethod
    def end_transaction() -> str:
        return "COMMIT"

    @staticmethod
    def quote_identifier(name: str) -> str:
        return f'"{name}"'

    @staticmethod
    def insert(row_values: Sequence[Any]) -> str:
        literals = [_literal_value(v, use_boolean_keywords=True) for v in row_values]
        return "VALUES (" + ", ".join(literals) + ")"


class SQLiteDialect(_SQLDialectGenerator):
    """SQLite: BEGIN TRANSACTION, COMMIT, unquoted identifiers, 1/0 booleans."""

    @staticmethod
    def dialect() -> str:
        return "sqlite"

    @staticmethod
    def begin_transaction() -> str:
        return "BEGIN TRANSACTION"

    @staticmethod
    def end_transaction() -> str:
        return "COMMIT"

    @staticmethod
    def quote_identifier(name: str) -> str:
        return name

    @staticmethod
    def insert(row_values: Sequence[Any]) -> str:
        literals = [_literal_value(v, use_boolean_keywords=False) for v in row_values]
        return "VALUES (" + ", ".join(literals) + ")"


class SnowflakeDialect(_SQLDialectGenerator):
    """Snowflake: BEGIN, COMMIT, double-quoted identifiers, TRUE/FALSE."""

    @staticmethod
    def dialect() -> str:
        return "snowflake"

    @staticmethod
    def begin_transaction() -> str:
        return "BEGIN"

    @staticmethod
    def end_transaction() -> str:
        return "COMMIT"

    @staticmethod
    def quote_identifier(name: str) -> str:
        return f'"{name}"'

    @staticmethod
    def insert(row_values: Sequence[Any]) -> str:
        literals = [_literal_value(v, use_boolean_keywords=True) for v in row_values]
        return "VALUES (" + ", ".join(literals) + ")"


_DIALECT_REGISTRY: Dict[str, Type[_SQLDialectGenerator]] = {
    "ansi": AnsiSqlDialect,
    "postgresql": PostgreSQLDialect,
    "sqlite": SQLiteDialect,
    "snowflake": SnowflakeDialect,
}


def get_dialect(name: str) -> Type[_SQLDialectGenerator]:
    """
    Resolve a dialect name to its generator class.

    Args:
        name: Dialect name (e.g. 'ansi', 'postgresql'). Case-insensitive, stripped.

    Returns:
        The dialect class (use static methods on it).

    Raises:
        ValueError: If the dialect name is not supported.
    """
    key = name.strip().lower() if name else ""
    if not key:
        raise ValueError("Dialect name cannot be empty")
    if key not in _DIALECT_REGISTRY:
        raise ValueError(
            f"Unknown SQL dialect: {name!r}. "
            f"Supported: {', '.join(sorted(_DIALECT_REGISTRY.keys()))}"
        )
    return _DIALECT_REGISTRY[key]
