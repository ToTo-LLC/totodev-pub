# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Database migration file reader for DbJig protocol.

This module provides file-reading-only access to database migration files following
the DbJig naming convention. It does not execute migrations or connect to databases.
Instead, it yields executable SQL strings that callers can execute against any backend.

Key features:
- Supports SQL files (.sql) and data files (.csv, .tsv, .json, .yaml, .jsonl)
- Follows DbJig batch label convention (YYYY-MM-DD prefix, default "0000-00-00")
- Provides ordered access by batch then basename
- Generates INSERT statements from data files
- Handles both filesystem and in-memory sources (for testing)

Usage:
    # From filesystem
    reader = DbMigrationFileReader('/path/to/migrations')

    # From in-memory dict (for testing)
    reader = DbMigrationFileReader({
        "0000-00-00_schema.sql": "CREATE TABLE users (id INT, name TEXT);",
        "2024-01-15_users.csv": "id,name\\n1,Alice\\n2,Bob"
    })

    # Get batch labels
    batches = reader.batch_labels()

    # Get migration files with transaction wrappers (default)
    files = reader.migration_files()

    # Execute SQL
    for file in files:
        for chunk in file.iter_sql_chunks():
            execute_on_database(chunk.sql)

The rock-bottom simplest way to apply the entire migration in order is:

    reader.execute_all(sql_executor, batches=None)

**Warning:** Using ``batches=None`` runs every migration file from scratch. This is
a bad strategy if some migrations have already been applied (e.g. you will
re-run CREATE TABLE and hit errors, or duplicate data). Prefer tracking applied
batches and passing only new ones, or use a migration runner that does this for you.

Trivial example — combine one directory to a single SQL file (statements only,
with batch labels as comments)::

    from pathlib import Path
    from totodev_pub.dbjig_support.db_migration_file_reader import DbMigrationFileReader

    migrations_dir = Path("/path/to/migrations")
    out_path = Path("volatile/combined.sql")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    reader = DbMigrationFileReader(migrations_dir)
    files = reader.migration_files()  # includes BEGIN/COMMIT per batch

    current_batch = None
    with open(out_path, "w") as out:
        for file in files:
            if file.info.batch_label != current_batch:
                current_batch = file.info.batch_label
                out.write(f"-- batch: {current_batch}\n")
            for chunk in file.iter_sql_chunks():
                out.write(chunk.sql + ";\n")

    # Result: one SQL file with all statements (no source comments),
    # synthetic BEGIN/COMMIT and INSERTs included, batch labels as -- comments.
"""

import os
import re
import glob
import tempfile
from io import StringIO
from pathlib import Path
from dataclasses import dataclass
from typing import (
    Any, Callable, Dict, Iterator, List, Literal, Mapping, Optional, Type, Union
)

from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData

from totodev_pub.dbjig_support.db_migration_sql_dialects import (
    _SQLDialectGenerator,
    get_dialect,
)


# Default batch label for files without YYYY-MM-DD prefix
DEFAULT_BATCH_LABEL = "0000-00-00"

# Supported file extensions
SUPPORTED_SQL_SUFFIXES = {".sql"}
SUPPORTED_DATA_SUFFIXES = {".csv", ".tsv", ".json", ".yaml", ".yml", ".jsonl", ".ndjson"}
SUPPORTED_SUFFIXES = SUPPORTED_SQL_SUFFIXES | SUPPORTED_DATA_SUFFIXES

# Synthetic batch wrapper identifiers (for transaction boundaries)
BATCH_WRAPPER_BEGIN = "begin"
BATCH_WRAPPER_COMMIT = "commit"
SYNTHETIC_BASENAME_BEGIN = "<<begin_transaction>>"
SYNTHETIC_BASENAME_COMMIT = "<<commit_transaction>>"


@dataclass(frozen=True)
class SqlChunk:
    """
    A parsed SQL statement with optional preceding comment metadata.

    Attributes:
        sql: The executable SQL statement text (without trailing semicolon).
        start_line: 1-based line number where the SQL statement begins in the
            source file. 0 for auto-generated chunks (data-file INSERTs,
            synthetic BEGIN/COMMIT).
        preceding_comment: Contiguous comment lines immediately before the SQL
            statement, preserving original text (including -- or # prefixes).
            Empty list for auto-generated chunks.
        is_auto_generated: True for synthetic transaction wrappers (BEGIN/COMMIT)
            and for INSERT statements generated from data files. False for SQL
            statements parsed directly from .sql files.
    """
    sql: str
    start_line: int = 0
    preceding_comment: List[str] = None  # type: ignore[assignment]  # frozen + default below
    is_auto_generated: bool = False

    def __post_init__(self):
        # Frozen dataclass: use object.__setattr__ to set mutable default
        if self.preceding_comment is None:
            object.__setattr__(self, 'preceding_comment', [])


@dataclass(frozen=True)
class MigrationFileInfo:
    """
    Parsed components from a migration filename.

    Attributes:
        batch_label: Batch label (e.g., "2024-01-15" or "0000-00-00")
        entity_name: Table name for data files; None for SQL files
        suffix: File extension including dot (e.g., ".sql", ".csv")
        basename: Filename without path
        path: Filesystem path as string, or None for synthetic/in-memory sources
        is_synthetic: True for BEGIN/COMMIT wrappers
        batch_wrapper_type: Type of batch wrapper (BATCH_WRAPPER_BEGIN or BATCH_WRAPPER_COMMIT)
    """
    batch_label: str
    entity_name: Optional[str]
    suffix: str
    basename: str
    path: Optional[str]
    is_synthetic: bool = False
    batch_wrapper_type: Optional[Literal['begin', 'commit']] = None

    @property
    def is_supported(self) -> bool:
        """Check if file suffix is supported."""
        return self.suffix.lower() in SUPPORTED_SUFFIXES

    @property
    def is_sql_file(self) -> bool:
        """True if this is a SQL file."""
        return self.suffix.lower() in SUPPORTED_SQL_SUFFIXES

    @property
    def is_data_file(self) -> bool:
        """True if this is a data file."""
        return self.suffix.lower() in SUPPORTED_DATA_SUFFIXES


class MigrationFile:
    """
    Represents a single migration file.

    Provides access to file metadata and yields executable SQL strings.
    For filesystem data files, rows are streamed from disk by default.
    For in-memory sources, data is pre-loaded via LazyLoadedFileData and
    the ``stream_rows`` flag is set to False so cached data is used.
    """

    def __init__(
        self,
        info: MigrationFileInfo,
        source: str,
        stream_rows: bool = True,
        _preloaded_data: Optional[LazyLoadedFileData] = None,
        _sql_text: Optional[str] = None,
        _dialect: Optional[Type[_SQLDialectGenerator]] = None,
    ):
        """
        Initialize a migration file.

        Args:
            info: Parsed file metadata
            source: Filesystem path to the file (string)
            stream_rows: If True (default), data files are streamed row-by-row
                from disk via LazyLoadedFileData.iter_list(). If False, all
                rows are loaded into memory via as_list() (useful when the
                backing file may no longer exist).
            _preloaded_data: Optional pre-loaded LazyLoadedFileData with cached
                rows. Used by in-memory sources where the temp file has already
                been deleted.
            _sql_text: Optional raw SQL text for in-memory SQL files. When set,
                the SQL is parsed from this string instead of opening a file.
            _dialect: SQL dialect class for generated snippets (BEGIN/COMMIT, INSERT).
                Passed by DbMigrationFileReader; defaults to ANSI when None.
        """
        self._info = info
        self._source = source
        self._stream_rows = stream_rows
        self._preloaded_data = _preloaded_data
        self._sql_text = _sql_text
        self._dialect = _dialect if _dialect is not None else get_dialect("ansi")

    @property
    def info(self) -> MigrationFileInfo:
        """Get file metadata."""
        return self._info

    @property
    def is_data(self) -> bool:
        """True if this is a data file."""
        return self._info.is_data_file

    @property
    def is_synthetic(self) -> bool:
        """True if this is a synthetic batch wrapper."""
        return self._info.is_synthetic

    @property
    def column_names(self) -> Optional[List[str]]:
        """
        Return column names for data files (from LazyLoadedFileData headers).

        Returns None for SQL files and synthetic wrappers. For data files,
        returns the list of column names derived from the file headers
        (with spaces replaced by underscores, matching DbJig convention).

        This is useful for callers that need to auto-create tables before
        iterating INSERT statements from iter_sql_chunks().
        """
        if not self._info.is_data_file:
            return None

        if self._preloaded_data is not None:
            # Pre-loaded data (from in-memory source)
            rows = self._preloaded_data.as_list(mutable=True, ignore_comments=True)
            for row in rows:
                return [key.replace(' ', '_') for key in row.keys()]
            return None  # empty data
        else:
            # Filesystem source: peek at headers via LazyLoadedFileData
            lazy_data = LazyLoadedFileData(
                self._source, flex_header_limit=10, change_detection_secs=0
            )
            for row in lazy_data.iter_list(mutable=True, ignore_comments=True):
                return [key.replace(' ', '_') for key in row.keys()]
            return None  # empty data

    def iter_sql_chunks(self) -> Iterator[SqlChunk]:
        """
        Yield SqlChunk objects preserving comment metadata.

        For SQL files: preserves preceding comments and line numbers.
        For data files: yields SqlChunk with is_auto_generated=True.
        For synthetic wrappers: yields SqlChunk with is_auto_generated=True.

        Yields:
            SqlChunk objects containing SQL text plus metadata
        """
        # Handle synthetic batch wrappers
        if self._info.is_synthetic:
            if self._info.batch_wrapper_type == BATCH_WRAPPER_BEGIN:
                yield SqlChunk(
                    sql=self._dialect.begin_transaction(),
                    is_auto_generated=True,
                )
            elif self._info.batch_wrapper_type == BATCH_WRAPPER_COMMIT:
                yield SqlChunk(
                    sql=self._dialect.end_transaction(),
                    is_auto_generated=True,
                )
            return

        # Handle SQL files
        if self._info.is_sql_file:
            yield from self._iter_sql_file_chunks()
            return

        # Handle data files: yield one INSERT per row
        for insert_sql in self._generate_inserts_from_data_file():
            yield SqlChunk(sql=insert_sql, is_auto_generated=True)

    def _iter_sql_file_chunks(self) -> Iterator[SqlChunk]:
        """
        Parse and yield SqlChunk objects from a .sql file.

        Adapted from dbjig.py _chunk_sql_file logic.
        Preserves preceding comment lines and start line numbers.
        """
        if self._sql_text is not None:
            # In-memory SQL: parse from stored string
            yield from self._parse_sql_chunks(StringIO(self._sql_text))
        else:
            # Filesystem SQL: read from file
            with open(self._source, 'r') as f:
                yield from self._parse_sql_chunks(f)

    @staticmethod
    def _parse_sql_chunks(sql_file) -> Iterator[SqlChunk]:
        """
        Parse SQL statements from a file-like object, preserving comment metadata.

        Handles multi-line statements, removes semicolons, tracks preceding
        comment lines and start line numbers for each statement.

        Yields:
            SqlChunk objects with sql, start_line, and preceding_comment
        """
        sql_lines: List[str] = []
        preceding_comment_lines: List[str] = []
        start_line = 0

        for line_number, line in enumerate(sql_file, start=1):
            stripped_line = line.strip()
            line = line.rstrip()

            # Skip blank lines when not accumulating SQL -- also reset comments
            if stripped_line == "" and not sql_lines:
                preceding_comment_lines.clear()
                continue
            elif stripped_line.startswith(('--', '#')) or stripped_line == '':
                if not sql_lines:
                    preceding_comment_lines.append(line)
                continue
            else:
                start_line = line_number if start_line == 0 else start_line

            # Collect SQL lines
            if not stripped_line.endswith(';'):
                sql_lines.append(line)
            else:
                # Hit semicolon - complete statement
                sql_lines.append(line[:-1])  # Remove semicolon
                statement = '\n'.join(sql_lines).rstrip()
                if statement:
                    yield SqlChunk(
                        sql=statement,
                        start_line=start_line,
                        preceding_comment=list(preceding_comment_lines),
                    )
                start_line, sql_lines, preceding_comment_lines = 0, [], []

        # Handle file without final semicolon
        if sql_lines:
            statement = '\n'.join(sql_lines).rstrip()
            if statement:
                yield SqlChunk(
                    sql=statement,
                    start_line=start_line,
                    preceding_comment=list(preceding_comment_lines),
                )

    def _generate_inserts_from_data_file(self) -> Iterator[str]:
        """
        Generate INSERT statements from a data file.

        Uses LazyLoadedFileData for both streaming (iter_list) and pre-loaded
        (as_list) access, controlled by the stream_rows flag.
        """
        table_name = self._info.entity_name

        if self._preloaded_data is not None:
            # Pre-loaded data (from in-memory source); file may no longer exist
            rows = self._preloaded_data.as_list(mutable=True, ignore_comments=True)
        elif self._stream_rows:
            # Filesystem source: stream rows from disk
            lazy_data = LazyLoadedFileData(self._source, flex_header_limit=10)
            rows = lazy_data.iter_list(mutable=True, ignore_comments=True)
        else:
            # Filesystem source, non-streaming: load all rows into memory
            lazy_data = LazyLoadedFileData(
                self._source, flex_header_limit=10, change_detection_secs=0
            )
            rows = lazy_data.as_list(mutable=True, ignore_comments=True)

        quoted_table = self._dialect.quote_identifier(table_name)
        for row in rows:
            columns = list(row.keys())
            # Convert empty strings to None (NULL) -- standard database migration
            # semantics: an empty cell in a CSV/TSV should map to SQL NULL, not ''.
            row_values = [(None if v == '' else v) for v in (row[col] for col in columns)]
            quoted_cols = ", ".join(self._dialect.quote_identifier(c) for c in columns)
            values_clause = self._dialect.insert(row_values)
            yield f"INSERT INTO {quoted_table} ({quoted_cols}) {values_clause}"

    def __repr__(self) -> str:
        """String representation."""
        if self._info.is_synthetic:
            return f"MigrationFile(synthetic={self._info.batch_wrapper_type!r}, batch={self._info.batch_label!r})"
        return f"MigrationFile(basename={self._info.basename!r}, batch={self._info.batch_label!r})"

    def __lt__(self, other: 'MigrationFile') -> bool:
        """Sort by batch label, then basename."""
        if not isinstance(other, MigrationFile):
            return NotImplemented
        return (
            self._info.batch_label < other._info.batch_label or
            (self._info.batch_label == other._info.batch_label and
             self._info.basename < other._info.basename)
        )

    def __eq__(self, other: object) -> bool:
        """Equality comparison."""
        if not isinstance(other, MigrationFile):
            return NotImplemented
        return (
            self._info.batch_label == other._info.batch_label and
            self._info.basename == other._info.basename
        )


class DbMigrationFileReader:
    """
    Discovers, orders, and provides access to database migration files.

    This class reads migration files following the DbJig protocol but does not
    execute them or connect to databases. It yields executable SQL strings that
    callers can execute against any database backend.

    Supports:
    - SQL files (.sql)
    - Data files (.csv, .tsv, .json, .yaml, .jsonl)
    - Filesystem sources (paths, globs, directories)
    - In-memory sources (dict of filename -> content)

    File naming convention:
    - SQL files: [YYYY-MM-DD_]name.sql
    - Data files: [YYYY-MM-DD_]tablename.{csv,tsv,json,yaml,jsonl}
    - Files without YYYY-MM-DD prefix get batch "0000-00-00"

    Usage:
        # From filesystem
        reader = DbMigrationFileReader('/path/to/migrations')

        # From multiple sources
        reader = DbMigrationFileReader([
            '/path/to/migrations',
            '/other/path/*.sql'
        ])

        # From in-memory dict
        reader = DbMigrationFileReader({
            "schema.sql": "CREATE TABLE users (id INT);",
            "users.csv": "id,name\\n1,Alice"
        })

        # Get batch labels
        batches = reader.batch_labels()
        recent = reader.batch_labels(after='2024-01-01')

        # Get migration files with transaction wrappers (default: one transaction per batch)
        files = reader.migration_files()
        files = reader.migration_files(batches=['2024-01-15'])
        files = reader.migration_files(add_transaction_files=False)  # no BEGIN/COMMIT

        # Execute SQL
        for file in files:
            for chunk in file.iter_sql_chunks():
                db.execute(chunk.sql)
    """

    def __init__(
        self,
        sources: Union[str, Path, List[Union[str, Path]], Mapping[str, str]],
        sql_dialect: Optional[str] = None
    ):
        """
        Initialize the migration file reader.

        Args:
            sources: Migration file sources. Can be:
                    - Single path (str or Path)
                    - List of paths/globs
                    - Dict mapping filename -> content (for in-memory testing)
            sql_dialect: SQL dialect for generated snippets (transaction syntax,
                identifier quoting, INSERT literal formatting). Use "ansi", "postgresql",
                "sqlite", or "snowflake". Defaults to "ansi" when None.

        Raises:
            TypeError: If sources is an unsupported type
            ValueError: If a data file has no table name (e.g. "2024-01-15_.csv"),
                or if sql_dialect is not supported
        """
        self._dialect = get_dialect("ansi" if sql_dialect is None else sql_dialect)

        # Normalize sources to list of MigrationFile objects
        if isinstance(sources, Mapping):
            # In-memory dict: {"filename": "content", ...}
            self._sources = self._load_from_dict(sources)
        elif isinstance(sources, (str, os.PathLike)):
            # Single path
            self._sources = self._expand_paths([sources])
        elif isinstance(sources, list):
            # List of paths/globs
            self._sources = self._expand_paths(sources)
        else:
            raise TypeError(f"Unsupported sources type: {type(sources)}")

        # Deduplicate by (batch_label, basename) so the same file is never run twice
        seen: Dict[tuple, MigrationFile] = {}
        for f in self._sources:
            key = (f.info.batch_label, f.info.basename)
            if key not in seen:
                seen[key] = f
        self._sources = list(seen.values())

        # Sort by batch then basename
        self._sources.sort()

    def batch_labels(self, after: Optional[str] = None) -> List[str]:
        """
        Get list of batch labels in sorted order.

        Collects unique batch labels from all migration files, optionally
        filtering to those lexicographically after a given label, then
        returns them sorted.

        Args:
            after: Optional batch label. If provided, only returns batches
                  lexicographically after this value (exclusive).

        Returns:
            Sorted list of unique batch labels

        Example:
            >>> reader.batch_labels()
            ['0000-00-00', '2024-01-15', '2024-02-01']
            >>> reader.batch_labels(after='2024-01-15')
            ['2024-02-01']
        """
        unique = {f.info.batch_label for f in self._sources}
        if after is not None:
            unique = {b for b in unique if b > after}
        return sorted(unique)

    def migration_files(
        self,
        batches: Optional[List[str]] = None,
        add_transaction_files: bool = True
    ) -> List[MigrationFile]:
        """
        Get ordered migration files, with synthetic transaction wrappers by default.

        Returns the list of migration files in order (by batch label, then basename).
        By default (add_transaction_files=True), synthetic "files" that yield BEGIN
        TRANSACTION and COMMIT are inserted around each batch so each batch runs in
        one transaction. Set add_transaction_files=False to get only the real
        migration files with no wrappers.

        Args:
            batches: Optional list of batch labels to include. If None, includes all.
            add_transaction_files: If True (default), insert synthetic BEGIN/COMMIT
                wrappers around each batch so callers get one transaction per batch.

        Returns:
            Ordered list of MigrationFile objects (real files plus optional wrappers)

        Example:
            >>> files = reader.migration_files(batches=['2024-01-15'])
            >>> for f in files:
            ...     print(f.info.basename)
            <<begin_transaction>>
            2024-01-15_schema.sql
            2024-01-15_users.csv
            <<commit_transaction>>
        """
        # Filter by batch if specified
        if batches is not None:
            batch_set = set(batches)
            filtered = [f for f in self._sources if f.info.batch_label in batch_set]
        else:
            filtered = self._sources[:]

        if not add_transaction_files:
            return filtered

        # Insert BEGIN/COMMIT wrappers
        result = []
        current_batch = None

        for file in filtered:
            batch = file.info.batch_label

            # New batch - close previous and open new
            if batch != current_batch:
                if current_batch is not None:
                    # Close previous batch
                    result.append(self._create_commit_wrapper(current_batch))

                # Open new batch
                result.append(self._create_begin_wrapper(batch))
                current_batch = batch

            result.append(file)

        # Close final batch
        if current_batch is not None:
            result.append(self._create_commit_wrapper(current_batch))

        return result

    def execute_all(
        self,
        sql_executor: Callable[[str], Any],
        batches: Optional[List[str]] = None,
        add_transaction_files: bool = True
    ) -> Optional[str]:
        """
        Run every SQL statement in order via the given executor.

        Convenience method that uses the same file and batch order as
        :meth:`migration_files`, but executes each yielded SQL string by
        calling ``sql_executor(sql)``. If you need atomic batches, ensure
        your database connection is not in auto-commit mode so that
        BEGIN/COMMIT (when ``add_transaction_files=True``) control transaction
        boundaries.

        Args:
            sql_executor: Callable that accepts a single SQL string and
                executes it (e.g. a thin wrapper around your DBMS execute).
            batches: Optional list of batch labels to include. If None,
                includes all batches.
            add_transaction_files: If True (default), use synthetic BEGIN/COMMIT
                wrappers around each batch (same as :meth:`migration_files`).

        Returns:
            The last batch label that was successfully committed (when
            add_transaction_files=True), or the batch of the last file processed
            (when add_transaction_files=False). None if no statements were run.

        Raises:
            Exception: Any exception raised by ``sql_executor`` is
                re-raised with extra context: batch label, file basename,
                and statement index.
        """
        files = self.migration_files(batches=batches, add_transaction_files=add_transaction_files)
        last_applied_batch: Optional[str] = None

        for file in files:
            batch_label = file.info.batch_label
            for statement_index, chunk in enumerate(file.iter_sql_chunks()):
                try:
                    sql_executor(chunk.sql)
                except Exception as e:
                    raise RuntimeError(
                        f"Migration failed at batch {batch_label!r}, "
                        f"file {file.info.basename!r}, "
                        f"statement index {statement_index}: {e}"
                    ) from e
            if add_transaction_files and file.info.is_synthetic and file.info.batch_wrapper_type == BATCH_WRAPPER_COMMIT:
                last_applied_batch = batch_label
            elif not add_transaction_files:
                last_applied_batch = batch_label

        return last_applied_batch

    @classmethod
    def parse_filename(cls, filename: Union[str, Path]) -> MigrationFileInfo:
        """
        Parse a migration filename into its components.

        Args:
            filename: Filename or path to parse

        Returns:
            MigrationFileInfo with parsed components

        Example:
            >>> info = DbMigrationFileReader.parse_filename("2024-01-15_users.csv")
            >>> info.batch_label
            '2024-01-15'
            >>> info.entity_name
            'users'
            >>> info.suffix
            '.csv'
        """
        # Extract basename and suffix from the full basename (so "2024-01-15_.csv" -> .csv)
        basename = os.path.basename(str(filename))
        _root, suffix = os.path.splitext(basename)
        suffix_lower = suffix.lower()

        # Try to match YYYY-MM-DD prefix
        match = re.match(r'^(\d{4}-\d{2}-\d{2})[_-]?(.*)', basename)
        if match:
            batch_label = match.group(1)
            remainder = match.group(2)
            # Entity name is the part before the extension (e.g. "users" from "users.csv")
            entity_name = remainder[: -len(suffix)] if suffix and remainder.endswith(suffix) else os.path.splitext(remainder)[0]
        else:
            batch_label = DEFAULT_BATCH_LABEL
            entity_name = _root if suffix else ""

        # For SQL files, entity_name should be None
        if suffix_lower in SUPPORTED_SQL_SUFFIXES:
            entity_name = None

        # If entity_name is empty string, set to None
        if entity_name == "":
            entity_name = None

        # Data files must have a table name (e.g. "users" from "2024-01-15_users.csv")
        if suffix_lower in SUPPORTED_DATA_SUFFIXES and entity_name is None:
            raise ValueError(
                f"Data file must have a table name: {basename!r}. "
                "Use a name like YYYY-MM-DD_tablename.csv or tablename.csv."
            )

        return MigrationFileInfo(
            batch_label=batch_label,
            entity_name=entity_name,
            suffix=suffix,
            basename=basename,
            path=str(filename) if filename else None
        )

    def _load_from_dict(self, file_dict: Mapping[str, str]) -> List[MigrationFile]:
        """
        Load migration files from a dict of filename -> content.

        For data files, the content is written to a temporary file so that
        LazyLoadedFileData can parse it, then pre-loaded into memory via
        as_list(). The temp file is deleted immediately after loading.

        For SQL files, the raw SQL text is stored directly on the MigrationFile
        (no temp file needed).

        Args:
            file_dict: Dict mapping filename to file content

        Returns:
            List of MigrationFile objects
        """
        files = []

        for name, content in file_dict.items():
            if not isinstance(name, str):
                raise TypeError("Keys of file_dict must be strings")
            if not isinstance(content, str):
                raise TypeError("Values of file_dict must be strings")

            info = self.parse_filename(name)
            # Override path to None since this is an in-memory source
            info = MigrationFileInfo(
                batch_label=info.batch_label,
                entity_name=info.entity_name,
                suffix=info.suffix,
                basename=info.basename,
                path=None,
                is_synthetic=info.is_synthetic,
                batch_wrapper_type=info.batch_wrapper_type,
            )

            if info.is_sql_file:
                # SQL files: store raw text, no temp file needed
                files.append(MigrationFile(
                    info, source=name, stream_rows=False, _sql_text=content,
                    _dialect=self._dialect,
                ))
            else:
                # Data files: write to temp file, preload via LazyLoadedFileData
                suffix = info.suffix  # e.g. ".csv"
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix=suffix, delete=False, encoding='utf-8'
                ) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name

                try:
                    lazy_data = LazyLoadedFileData(
                        tmp_path, flex_header_limit=10, change_detection_secs=0
                    )
                    # Force data into memory so the temp file can be deleted
                    lazy_data.as_list(mutable=True, ignore_comments=True)
                finally:
                    os.unlink(tmp_path)

                files.append(MigrationFile(
                    info, source=name, stream_rows=False,
                    _preloaded_data=lazy_data,
                    _dialect=self._dialect,
                ))

        return files

    def _expand_paths(self, sources: List[Union[str, Path]]) -> List[MigrationFile]:
        """
        Expand paths, globs, and directories into MigrationFile objects.

        Args:
            sources: List of filesystem paths or globs

        Returns:
            List of MigrationFile objects
        """
        files = []

        for source in sources:
            source_str = str(source)

            # Handle directories
            if os.path.isdir(source_str):
                source_str = os.path.join(source_str, "*")

            # Handle globs
            if '*' in source_str or '?' in source_str or '[' in source_str:
                for path in glob.glob(source_str):
                    path = os.path.abspath(path)
                    if os.path.isfile(path):
                        info = self.parse_filename(path)
                        if info.is_supported:
                            files.append(MigrationFile(info, path, _dialect=self._dialect))
            else:
                # Direct file path
                path = os.path.abspath(source_str)
                if os.path.isfile(path):
                    info = self.parse_filename(path)
                    if info.is_supported:
                        files.append(MigrationFile(info, path, _dialect=self._dialect))

        return files

    def _create_begin_wrapper(self, batch_label: str) -> MigrationFile:
        """Create synthetic BEGIN TRANSACTION wrapper for batch."""
        info = MigrationFileInfo(
            batch_label=batch_label,
            entity_name=None,
            suffix="",
            basename=SYNTHETIC_BASENAME_BEGIN,
            path=None,
            is_synthetic=True,
            batch_wrapper_type=BATCH_WRAPPER_BEGIN
        )
        return MigrationFile(info, source=SYNTHETIC_BASENAME_BEGIN, _dialect=self._dialect)

    def _create_commit_wrapper(self, batch_label: str) -> MigrationFile:
        """Create synthetic COMMIT wrapper for batch."""
        info = MigrationFileInfo(
            batch_label=batch_label,
            entity_name=None,
            suffix="",
            basename=SYNTHETIC_BASENAME_COMMIT,
            path=None,
            is_synthetic=True,
            batch_wrapper_type=BATCH_WRAPPER_COMMIT
        )
        return MigrationFile(info, source=SYNTHETIC_BASENAME_COMMIT, _dialect=self._dialect)
