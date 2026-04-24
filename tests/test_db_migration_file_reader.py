# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for DbMigrationFileReader and MigrationFile.

Covers parse_filename, batch_labels, migration_files, iter_sql_chunks,
deduplication, and validation (e.g. data file without table name).
"""

import os
import tempfile
import pytest

from totodev_pub.dbjig_support.db_migration_file_reader import (
    BATCH_WRAPPER_BEGIN,
    BATCH_WRAPPER_COMMIT,
    DEFAULT_BATCH_LABEL,
    DbMigrationFileReader,
    MigrationFile,
    MigrationFileInfo,
    SqlChunk,
    SYNTHETIC_BASENAME_BEGIN,
    SYNTHETIC_BASENAME_COMMIT,
)


class TestParseFilename:
    """Tests for DbMigrationFileReader.parse_filename."""

    def test_sql_file_with_date_prefix(self):
        info = DbMigrationFileReader.parse_filename("2024-01-15_schema.sql")
        assert info.batch_label == "2024-01-15"
        assert info.entity_name is None
        assert info.suffix == ".sql"
        assert info.basename == "2024-01-15_schema.sql"
        assert info.is_sql_file
        assert not info.is_data_file

    def test_data_file_with_date_prefix(self):
        info = DbMigrationFileReader.parse_filename("2024-01-15_users.csv")
        assert info.batch_label == "2024-01-15"
        assert info.entity_name == "users"
        assert info.suffix == ".csv"
        assert info.is_data_file

    def test_no_date_prefix_gets_default_batch(self):
        info = DbMigrationFileReader.parse_filename("schema.sql")
        assert info.batch_label == DEFAULT_BATCH_LABEL
        assert info.entity_name is None

    def test_path_is_string_for_path_input(self):
        info = DbMigrationFileReader.parse_filename("/abs/path/2024-01-15_foo.sql")
        assert info.path == "/abs/path/2024-01-15_foo.sql"
        assert isinstance(info.path, str)

    def test_data_file_without_table_name_raises(self):
        # e.g. "2024-01-15_.csv" has no table name before the extension
        with pytest.raises(ValueError, match="Data file must have a table name"):
            DbMigrationFileReader.parse_filename("2024-01-15_.csv")


class TestBatchLabels:
    """Tests for batch_labels()."""

    def test_unique_sorted_labels(self):
        reader = DbMigrationFileReader({
            "2024-02-01_b.sql": "x",
            "2024-01-15_a.sql": "y",
            "0000-00-00_schema.sql": "z",
        })
        assert reader.batch_labels() == ["0000-00-00", "2024-01-15", "2024-02-01"]

    def test_after_filters(self):
        reader = DbMigrationFileReader({
            "2024-02-01_b.sql": "x",
            "2024-01-15_a.sql": "y",
            "0000-00-00_schema.sql": "z",
        })
        assert reader.batch_labels(after="2024-01-15") == ["2024-02-01"]
        assert reader.batch_labels(after="2024-01-14") == ["2024-01-15", "2024-02-01"]


class TestMigrationFiles:
    """Tests for migration_files()."""

    def test_default_includes_wrappers(self):
        reader = DbMigrationFileReader({
            "2024-01-15_schema.sql": "CREATE TABLE t (x INT);",
        })
        files = reader.migration_files()
        basenames = [f.info.basename for f in files]
        assert basenames[0] == SYNTHETIC_BASENAME_BEGIN
        assert basenames[1] == "2024-01-15_schema.sql"
        assert basenames[2] == SYNTHETIC_BASENAME_COMMIT

    def test_add_transaction_files_false_no_wrappers(self):
        reader = DbMigrationFileReader({
            "2024-01-15_schema.sql": "CREATE TABLE t (x INT);",
        })
        files = reader.migration_files(add_transaction_files=False)
        assert len(files) == 1
        assert files[0].info.basename == "2024-01-15_schema.sql"

    def test_batches_filter(self):
        reader = DbMigrationFileReader({
            "2024-01-15_a.sql": "x",
            "2024-02-01_b.sql": "y",
        })
        files = reader.migration_files(batches=["2024-01-15"])
        basenames = [f.info.basename for f in files]
        assert SYNTHETIC_BASENAME_BEGIN in basenames
        assert "2024-01-15_a.sql" in basenames
        assert "2024-02-01_b.sql" not in basenames

    def test_deduplication_by_batch_and_basename(self):
        # Two sources that both include the same file -> only one MigrationFile
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "2024-01-15_one.sql")
            with open(path, "w") as f:
                f.write("SELECT 1;")
            reader = DbMigrationFileReader([path, path])
            files = reader.migration_files(add_transaction_files=False)
            assert len(files) == 1
            assert files[0].info.basename == "2024-01-15_one.sql"


class TestIterSql:
    """Tests for MigrationFile.iter_sql_chunks()."""

    def test_sql_file_yields_statements(self):
        reader = DbMigrationFileReader({
            "0000-00-00_schema.sql": "CREATE TABLE t (x INT);\nCREATE TABLE u (y INT);",
        })
        files = reader.migration_files(add_transaction_files=False)
        assert len(files) == 1
        sql_list = [c.sql for c in files[0].iter_sql_chunks()]
        assert len(sql_list) == 2
        assert "CREATE TABLE t" in sql_list[0]
        assert "CREATE TABLE u" in sql_list[1]

    def test_synthetic_begin_commit(self):
        reader = DbMigrationFileReader({"2024-01-15_a.sql": "SELECT 1;"})
        files = reader.migration_files()
        sql_all = [c.sql for f in files for c in f.iter_sql_chunks()]
        assert "BEGIN TRANSACTION" in sql_all
        assert "COMMIT" in sql_all

    def test_data_file_yields_inserts(self):
        reader = DbMigrationFileReader({
            "2024-01-15_users.csv": "id,name\n1,Alice\n2,Bob",
        })
        files = reader.migration_files(add_transaction_files=False)
        assert len(files) == 1
        sql_list = [c.sql for c in files[0].iter_sql_chunks()]
        assert len(sql_list) == 2
        assert "INSERT INTO users" in sql_list[0]
        assert "1" in sql_list[0] and "Alice" in sql_list[0]


class TestPathConsistency:
    """Path is Optional[str]: filesystem path string or None."""

    def test_synthetic_has_no_path(self):
        reader = DbMigrationFileReader({"2024-01-15_a.sql": "x"})
        files = reader.migration_files()
        for f in files:
            if f.info.is_synthetic:
                assert f.info.path is None

    def test_in_memory_dict_path_is_none(self):
        reader = DbMigrationFileReader({"2024-01-15_a.sql": "x"})
        files = reader.migration_files(add_transaction_files=False)
        assert len(files) == 1
        assert files[0].info.path is None


class TestStreamRows:
    """Tests for stream_rows and in-memory preloading."""

    def test_in_memory_data_file_uses_preloaded_data(self):
        """Dict-sourced data files should work even though temp file is deleted."""
        reader = DbMigrationFileReader({
            "2024-01-15_users.csv": "id,name\n1,Alice\n2,Bob",
        })
        files = reader.migration_files(add_transaction_files=False)
        assert len(files) == 1
        # stream_rows should be False for dict-sourced files
        assert files[0]._stream_rows is False
        assert files[0]._preloaded_data is not None
        # Should yield correct INSERTs from preloaded data
        sql_list = [c.sql for c in files[0].iter_sql_chunks()]
        assert len(sql_list) == 2
        assert "INSERT INTO users" in sql_list[0]

    def test_in_memory_data_file_repeatable(self):
        """iter_sql_chunks() on a preloaded data file can be called multiple times."""
        reader = DbMigrationFileReader({
            "2024-01-15_items.csv": "id,label\n10,Widget\n20,Gadget",
        })
        files = reader.migration_files(add_transaction_files=False)
        first = [c.sql for c in files[0].iter_sql_chunks()]
        second = [c.sql for c in files[0].iter_sql_chunks()]
        assert first == second
        assert len(first) == 2

    def test_in_memory_sql_file_uses_sql_text(self):
        """Dict-sourced SQL files store raw text, no preloaded_data."""
        reader = DbMigrationFileReader({
            "schema.sql": "CREATE TABLE t (x INT);",
        })
        files = reader.migration_files(add_transaction_files=False)
        assert len(files) == 1
        assert files[0]._sql_text is not None
        assert files[0]._preloaded_data is None
        sql_list = [c.sql for c in files[0].iter_sql_chunks()]
        assert sql_list == ["CREATE TABLE t (x INT)"]

    def test_filesystem_file_streams_by_default(self):
        """Filesystem-sourced files have stream_rows=True by default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "2024-01-15_data.csv")
            with open(path, "w") as f:
                f.write("id,val\n1,a\n2,b")
            reader = DbMigrationFileReader([path])
            files = reader.migration_files(add_transaction_files=False)
            assert len(files) == 1
            assert files[0]._stream_rows is True
            assert files[0]._preloaded_data is None
            sql_list = [c.sql for c in files[0].iter_sql_chunks()]
            assert len(sql_list) == 2
            assert "INSERT INTO data" in sql_list[0]


class TestSqlDialect:
    """Tests for sql_dialect parameter and dialect-specific generated SQL."""

    def test_default_uses_ansi(self):
        """Default (None) produces ANSI: BEGIN TRANSACTION, COMMIT, unquoted identifiers."""
        reader = DbMigrationFileReader({
            "2024-01-15_schema.sql": "CREATE TABLE t (x INT);",
        })
        files = reader.migration_files()
        sql_all = " ".join(c.sql for f in files for c in f.iter_sql_chunks())
        assert "BEGIN TRANSACTION" in sql_all
        assert "COMMIT" in sql_all

    def test_ansi_explicit_same_as_default(self):
        """Explicit sql_dialect='ansi' matches default: unquoted identifiers."""
        reader = DbMigrationFileReader({
            "2024-01-15_users.csv": "id,name\n1,Alice",
        }, sql_dialect="ansi")
        files = reader.migration_files(add_transaction_files=False)
        sql_list = [c.sql for c in files[0].iter_sql_chunks()]
        assert "INSERT INTO users (id, name) VALUES" in sql_list[0]
        assert "'Alice'" in sql_list[0]
        assert '"users"' not in sql_list[0]

    def test_postgresql_start_transaction_and_commit(self):
        """PostgreSQL uses START TRANSACTION and COMMIT."""
        reader = DbMigrationFileReader({
            "2024-01-15_a.sql": "SELECT 1;",
        }, sql_dialect="postgresql")
        files = reader.migration_files()
        sql_all = " ".join(c.sql for f in files for c in f.iter_sql_chunks())
        assert "START TRANSACTION" in sql_all
        assert "COMMIT" in sql_all
        assert "BEGIN TRANSACTION" not in sql_all

    def test_postgresql_quoted_identifiers(self):
        """PostgreSQL double-quotes table and column identifiers."""
        reader = DbMigrationFileReader({
            "2024-01-15_flags.csv": "id,active\n1,true\n2,false",
        }, sql_dialect="postgresql")
        files = reader.migration_files(add_transaction_files=False)
        sql_list = [c.sql for c in files[0].iter_sql_chunks()]
        first_insert = sql_list[0]
        assert '"flags"' in first_insert
        assert '"id"' in first_insert
        assert '"active"' in first_insert

    def test_sqlite_begin_transaction_and_commit(self):
        """SQLite uses BEGIN TRANSACTION and COMMIT, unquoted identifiers."""
        reader = DbMigrationFileReader({
            "2024-01-15_users.csv": "id,name\n1,Alice",
        }, sql_dialect="sqlite")
        files = reader.migration_files(add_transaction_files=False)
        sql_list = [c.sql for c in files[0].iter_sql_chunks()]
        assert "INSERT INTO users (id, name) VALUES" in sql_list[0]
        assert '"users"' not in sql_list[0]

    def test_snowflake_begin_and_commit(self):
        """Snowflake uses BEGIN and COMMIT, double-quoted identifiers."""
        reader = DbMigrationFileReader({
            "2024-01-15_a.sql": "SELECT 1;",
        }, sql_dialect="snowflake")
        files = reader.migration_files()
        sql_all = " ".join(c.sql for f in files for c in f.iter_sql_chunks())
        assert "BEGIN" in sql_all
        assert "COMMIT" in sql_all
        assert "START TRANSACTION" not in sql_all
        reader2 = DbMigrationFileReader({
            "2024-01-15_t.csv": "id,flag\n1,true",
        }, sql_dialect="snowflake")
        files2 = reader2.migration_files(add_transaction_files=False)
        sql_list = [c.sql for c in files2[0].iter_sql_chunks()]
        assert '"t"' in sql_list[0]
        assert '"id"' in sql_list[0] and '"flag"' in sql_list[0]

    def test_unknown_dialect_raises(self):
        """Unknown or empty sql_dialect raises ValueError."""
        with pytest.raises(ValueError, match="Unknown SQL dialect"):
            DbMigrationFileReader({"a.sql": "x"}, sql_dialect="unknown")
        with pytest.raises(ValueError, match="Dialect name cannot be empty"):
            DbMigrationFileReader({"a.sql": "x"}, sql_dialect="")


class TestIterSqlChunks:
    """Tests for MigrationFile.iter_sql_chunks()."""

    def test_sql_chunks_preserve_preceding_comments(self):
        """SQL file chunks should preserve preceding comment lines."""
        reader = DbMigrationFileReader({
            "schema.sql": (
                "-- This creates the users table\n"
                "-- It has two columns\n"
                "CREATE TABLE users (id INT, name TEXT);\n"
            ),
        })
        files = reader.migration_files(add_transaction_files=False)
        chunks = list(files[0].iter_sql_chunks())
        assert len(chunks) == 1
        assert chunks[0].sql == "CREATE TABLE users (id INT, name TEXT)"
        assert len(chunks[0].preceding_comment) == 2
        assert "-- This creates the users table" in chunks[0].preceding_comment[0]
        assert "-- It has two columns" in chunks[0].preceding_comment[1]
        assert chunks[0].is_auto_generated is False

    def test_sql_chunks_carry_correct_start_line(self):
        """SQL file chunks should have correct start_line values."""
        reader = DbMigrationFileReader({
            "schema.sql": (
                "-- comment\n"
                "CREATE TABLE t1 (x INT);\n"
                "\n"
                "CREATE TABLE t2 (y INT);\n"
            ),
        })
        files = reader.migration_files(add_transaction_files=False)
        chunks = list(files[0].iter_sql_chunks())
        assert len(chunks) == 2
        assert chunks[0].start_line == 2  # line 1 is comment, SQL starts line 2
        assert chunks[1].start_line == 4

    def test_pquery_style_chunks_preserve_comments(self):
        """Parameterized-query-style chunks preserve comment + :param SQL intact."""
        reader = DbMigrationFileReader({
            "queries.sql": (
                "CREATE TABLE fruits (name TEXT, color TEXT);\n"
                "\n"
                "-- fruits_by_color\n"
                "-- Returns fruits matching given color\n"
                "SELECT * FROM fruits WHERE color = :color;\n"
            ),
        })
        files = reader.migration_files(add_transaction_files=False)
        chunks = list(files[0].iter_sql_chunks())
        assert len(chunks) == 2
        # First chunk: CREATE TABLE
        assert "CREATE TABLE" in chunks[0].sql
        assert chunks[0].preceding_comment == []
        # Second chunk: parameterized query with comments
        assert ":color" in chunks[1].sql
        assert len(chunks[1].preceding_comment) == 2
        assert "fruits_by_color" in chunks[1].preceding_comment[0]
        assert "Returns fruits" in chunks[1].preceding_comment[1]
        assert chunks[1].is_auto_generated is False

    def test_data_file_chunks_are_auto_generated(self):
        """Data file chunks should have is_auto_generated=True and empty comments."""
        reader = DbMigrationFileReader({
            "2024-01-15_users.csv": "id,name\n1,Alice\n2,Bob",
        })
        files = reader.migration_files(add_transaction_files=False)
        chunks = list(files[0].iter_sql_chunks())
        assert len(chunks) == 2
        for chunk in chunks:
            assert chunk.is_auto_generated is True
            assert chunk.preceding_comment == []
            assert chunk.start_line == 0
            assert "INSERT INTO" in chunk.sql

    def test_synthetic_wrapper_chunks_are_auto_generated(self):
        """Synthetic BEGIN/COMMIT chunks should have is_auto_generated=True."""
        reader = DbMigrationFileReader({"2024-01-15_a.sql": "SELECT 1;"})
        files = reader.migration_files()
        chunks = []
        for f in files:
            chunks.extend(f.iter_sql_chunks())
        # Should have BEGIN, SELECT 1, COMMIT
        assert len(chunks) == 3
        # BEGIN
        assert chunks[0].is_auto_generated is True
        assert "BEGIN" in chunks[0].sql
        assert chunks[0].preceding_comment == []
        # SELECT 1
        assert chunks[1].is_auto_generated is False
        assert chunks[1].sql == "SELECT 1"
        # COMMIT
        assert chunks[2].is_auto_generated is True
        assert "COMMIT" in chunks[2].sql

    def test_blank_lines_reset_comments(self):
        """Blank lines between comment blocks and SQL reset the comment accumulator."""
        reader = DbMigrationFileReader({
            "schema.sql": (
                "-- orphan comment\n"
                "\n"
                "-- attached comment\n"
                "CREATE TABLE t (x INT);\n"
            ),
        })
        files = reader.migration_files(add_transaction_files=False)
        chunks = list(files[0].iter_sql_chunks())
        assert len(chunks) == 1
        # Only the "attached comment" should be preserved (orphan was reset by blank line)
        assert len(chunks[0].preceding_comment) == 1
        assert "attached comment" in chunks[0].preceding_comment[0]

    def test_hash_comments_preserved(self):
        """Hash-style comments (#) should also be preserved."""
        reader = DbMigrationFileReader({
            "schema.sql": (
                "# This is a hash comment\n"
                "CREATE TABLE t (x INT);\n"
            ),
        })
        files = reader.migration_files(add_transaction_files=False)
        chunks = list(files[0].iter_sql_chunks())
        assert len(chunks) == 1
        assert len(chunks[0].preceding_comment) == 1
        assert "# This is a hash comment" in chunks[0].preceding_comment[0]

    def test_sql_file_no_trailing_semicolon(self):
        """SQL file without trailing semicolon still yields the statement."""
        reader = DbMigrationFileReader({
            "schema.sql": "CREATE TABLE t (x INT)",
        })
        files = reader.migration_files(add_transaction_files=False)
        chunks = list(files[0].iter_sql_chunks())
        assert len(chunks) == 1
        assert chunks[0].sql == "CREATE TABLE t (x INT)"

    def test_multiline_sql_start_line(self):
        """Multi-line SQL statement tracks start_line of first SQL line, not comment."""
        reader = DbMigrationFileReader({
            "schema.sql": (
                "-- comment line 1\n"
                "-- comment line 2\n"
                "CREATE TABLE t (\n"
                "    x INT,\n"
                "    y TEXT\n"
                ");\n"
            ),
        })
        files = reader.migration_files(add_transaction_files=False)
        chunks = list(files[0].iter_sql_chunks())
        assert len(chunks) == 1
        assert chunks[0].start_line == 3  # SQL starts on line 3


class TestColumnNames:
    """Tests for MigrationFile.column_names property."""

    def test_csv_data_file_returns_column_names(self):
        """CSV data file should return correct column names."""
        reader = DbMigrationFileReader({
            "2024-01-15_users.csv": "id,name,email\n1,Alice,a@b.com\n2,Bob,b@c.com",
        })
        files = reader.migration_files(add_transaction_files=False)
        assert len(files) == 1
        assert files[0].column_names == ["id", "name", "email"]

    def test_json_data_file_returns_column_names(self):
        """JSON data file should return correct column names."""
        reader = DbMigrationFileReader({
            "2024-01-15_items.json": '[{"id": 1, "label": "Widget"}, {"id": 2, "label": "Gadget"}]',
        })
        files = reader.migration_files(add_transaction_files=False)
        assert len(files) == 1
        assert files[0].column_names == ["id", "label"]

    def test_sql_file_returns_none(self):
        """SQL file should return None for column_names."""
        reader = DbMigrationFileReader({
            "schema.sql": "CREATE TABLE t (x INT);",
        })
        files = reader.migration_files(add_transaction_files=False)
        assert files[0].column_names is None

    def test_synthetic_wrapper_returns_none(self):
        """Synthetic wrappers should return None for column_names."""
        reader = DbMigrationFileReader({"2024-01-15_a.sql": "SELECT 1;"})
        files = reader.migration_files()
        for f in files:
            if f.is_synthetic:
                assert f.column_names is None

    def test_column_names_with_spaces_replaced(self):
        """Column names with spaces should have them replaced with underscores."""
        reader = DbMigrationFileReader({
            "2024-01-15_items.csv": "item id,full name\n1,Alice\n2,Bob",
        })
        files = reader.migration_files(add_transaction_files=False)
        assert files[0].column_names == ["item_id", "full_name"]

    def test_filesystem_csv_returns_column_names(self):
        """Filesystem-sourced CSV should return column names without consuming rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "2024-01-15_data.csv")
            with open(path, "w") as f:
                f.write("id,val,category\n1,a,x\n2,b,y")
            reader = DbMigrationFileReader([path])
            files = reader.migration_files(add_transaction_files=False)
            assert files[0].column_names == ["id", "val", "category"]
            # iter_sql_chunks should still work after column_names was called
            sql_list = [c.sql for c in files[0].iter_sql_chunks()]
            assert len(sql_list) == 2
