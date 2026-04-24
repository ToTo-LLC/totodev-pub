# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for SQL dialect generators (db_migration_sql_dialects).

Covers begin_transaction, end_transaction, quote_identifier, and insert()
for each dialect, plus get_dialect registry.
"""

import pytest

from totodev_pub.dbjig_support.db_migration_sql_dialects import (
    AnsiSqlDialect,
    PostgreSQLDialect,
    SQLiteDialect,
    SnowflakeDialect,
    _SQLDialectGenerator,
    get_dialect,
)


class TestGetDialect:
    """Tests for get_dialect() registry."""

    def test_ansi(self):
        assert get_dialect("ansi") is AnsiSqlDialect
        assert get_dialect("ANSI") is AnsiSqlDialect

    def test_postgresql(self):
        assert get_dialect("postgresql") is PostgreSQLDialect
        assert get_dialect("PostgreSQL") is PostgreSQLDialect

    def test_sqlite(self):
        assert get_dialect("sqlite") is SQLiteDialect

    def test_snowflake(self):
        assert get_dialect("snowflake") is SnowflakeDialect

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown SQL dialect"):
            get_dialect("mysql")
        with pytest.raises(ValueError, match="Unknown SQL dialect"):
            get_dialect("oracle")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Dialect name cannot be empty"):
            get_dialect("")
        with pytest.raises(ValueError, match="Dialect name cannot be empty"):
            get_dialect("   ")


class TestAnsiDialect:
    """AnsiSqlDialect: BEGIN TRANSACTION, COMMIT, unquoted, 1/0 booleans."""

    def test_dialect(self):
        assert AnsiSqlDialect.dialect() == "ansi"

    def test_begin_end_transaction(self):
        assert AnsiSqlDialect.begin_transaction() == "BEGIN TRANSACTION"
        assert AnsiSqlDialect.end_transaction() == "COMMIT"

    def test_quote_identifier(self):
        assert AnsiSqlDialect.quote_identifier("users") == "users"
        assert AnsiSqlDialect.quote_identifier("MyTable") == "MyTable"

    def test_insert_literals(self):
        # None, bool, int, str
        out = AnsiSqlDialect.insert([None, True, False, 42, "hello"])
        assert out == "VALUES (NULL, 1, 0, 42, 'hello')"

    def test_insert_string_escaping(self):
        out = AnsiSqlDialect.insert(["O'Brien"])
        assert out == "VALUES ('O''Brien')"

    def test_insert_sql_expression_passthrough(self):
        """Strings that look like SQL function calls are passed through unquoted."""
        out = AnsiSqlDialect.insert(["DATE('2023-07-15')"])
        assert out == "VALUES (DATE('2023-07-15'))"
        out2 = AnsiSqlDialect.insert(["TIMESTAMP('2023-07-15 10:00:00')"])
        assert out2 == "VALUES (TIMESTAMP('2023-07-15 10:00:00'))"

    def test_insert_normal_string_still_quoted(self):
        """Normal prose or mixed-case 'expression' is quoted, not passed through."""
        out = AnsiSqlDialect.insert(["Next friday ('2023-07-15')"])
        assert out == "VALUES ('Next friday (''2023-07-15'')')"


class TestLooksLikeSqlExpression:
    """Tests for _SQLDialectGenerator._looks_like_sql_expression()."""

    def test_date_expression(self):
        assert _SQLDialectGenerator._looks_like_sql_expression("DATE('2023-07-15')") is True

    def test_timestamp_expression(self):
        assert _SQLDialectGenerator._looks_like_sql_expression("TIMESTAMP('2023-07-15 10:00:00')") is True

    def test_leading_trailing_space_stripped(self):
        assert _SQLDialectGenerator._looks_like_sql_expression("  DATE('2023-07-15')  ") is True

    def test_lowercase_not_passthrough(self):
        assert _SQLDialectGenerator._looks_like_sql_expression("date('2023-07-15')") is False
        assert _SQLDialectGenerator._looks_like_sql_expression("Next friday ('2023-07-15')") is False

    def test_no_parens_not_passthrough(self):
        assert _SQLDialectGenerator._looks_like_sql_expression("DATE 2023-07-15") is False

    def test_empty_or_non_string_false(self):
        assert _SQLDialectGenerator._looks_like_sql_expression("") is False
        assert _SQLDialectGenerator._looks_like_sql_expression("   ") is False


class TestPostgreSQLDialect:
    """PostgreSQLDialect: START TRANSACTION, COMMIT, double-quote, TRUE/FALSE."""

    def test_dialect(self):
        assert PostgreSQLDialect.dialect() == "postgresql"

    def test_begin_end_transaction(self):
        assert PostgreSQLDialect.begin_transaction() == "START TRANSACTION"
        assert PostgreSQLDialect.end_transaction() == "COMMIT"

    def test_quote_identifier(self):
        assert PostgreSQLDialect.quote_identifier("users") == '"users"'
        assert PostgreSQLDialect.quote_identifier("MyTable") == '"MyTable"'

    def test_insert_boolean_keywords(self):
        out = PostgreSQLDialect.insert([None, True, False, 1, "a"])
        assert out == "VALUES (NULL, TRUE, FALSE, 1, 'a')"


class TestSQLiteDialect:
    """SQLiteDialect: BEGIN TRANSACTION, COMMIT, unquoted, 1/0 booleans."""

    def test_dialect(self):
        assert SQLiteDialect.dialect() == "sqlite"

    def test_begin_end_transaction(self):
        assert SQLiteDialect.begin_transaction() == "BEGIN TRANSACTION"
        assert SQLiteDialect.end_transaction() == "COMMIT"

    def test_quote_identifier(self):
        assert SQLiteDialect.quote_identifier("users") == "users"

    def test_insert_boolean_numeric(self):
        out = SQLiteDialect.insert([True, False])
        assert out == "VALUES (1, 0)"


class TestSnowflakeDialect:
    """SnowflakeDialect: BEGIN, COMMIT, double-quote, TRUE/FALSE."""

    def test_dialect(self):
        assert SnowflakeDialect.dialect() == "snowflake"

    def test_begin_end_transaction(self):
        assert SnowflakeDialect.begin_transaction() == "BEGIN"
        assert SnowflakeDialect.end_transaction() == "COMMIT"

    def test_quote_identifier(self):
        assert SnowflakeDialect.quote_identifier("users") == '"users"'

    def test_insert_boolean_keywords(self):
        out = SnowflakeDialect.insert([True, False])
        assert out == "VALUES (TRUE, FALSE)"
