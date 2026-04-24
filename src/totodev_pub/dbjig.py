# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import sqlite3
import argparse
from typing import Optional,List,Tuple,Dict,Generator,Callable,Any,Iterator,Iterable,Self,Union,IO
import os
import csv
import sys
import re
import time
from pydantic import BaseModel
from datetime import datetime
from contextlib import contextmanager
import tempfile
import threading
from .dbjig_support.tbdict import TableBackedDict
from .dbjig_support.db_migration_file_reader import (
    DbMigrationFileReader, MigrationFile, MigrationFileInfo, SqlChunk
)


class DbJig:
    """Support class to be used by applications for constructing a back-end
    SQLite database from a pile of sql scripts (*.sql) and standard-format text files
    such as CSV (*.csv), TSV (*.tsv), YAML (yaml), JSON (*.json) files.
    File extensions are used to identify format (see method upgrade() for gory
    explanation of loading conventions). Class also includes simple facilities
    or upward "migration" of the database.  No facilities exist to "undo" a
    migration.

    Applications using this class should create an instance at their startup 
    which will automatically create or upgrade the database before providing
    connections to it.  The simplest mode for this is to specify where the
    database file is to be found/created and a folder wherein the construction
    "recipe" files are to be found.  The class name comes from a carpentry tool.
    The database has an ability to upgrade itself by the addition of scripts.    

    Thread Safety:
    -------------
    This class is designed with thread safety in mind. Each DbJig instance maintains
    a single database connection that is tied to the thread that created it. When
    using DbJig in a multi-threaded environment:
    
    1. Each thread should have its own DbJig instance
    2. Use the copy() method to create a new instance for each thread
    3. Never share a single DbJig instance across multiple threads
    
    Connection Management:
    --------------------
    The db() method returns a database connection that should be properly closed
    when no longer needed. While the __del__() method will attempt to close
    connections during garbage collection, it's best practice to explicitly call
    close() when you're done with the connection to ensure proper cleanup.
    
    Example of proper usage in a multi-threaded environment:
    
    ```python
    # Main thread
    dbjig = DbJig("my_database.db")
    
    # Create a copy for a worker thread
    worker_dbjig = dbjig.copy()
    
    # Pass worker_dbjig to the worker thread
    worker_thread = threading.Thread(target=worker_function, args=(worker_dbjig,))
    worker_thread.start()
    
    # When done with the main thread's connection
    dbjig.close()
    ```
    
    Note that this class was designed for speed of development, not high performance.
    If you need something high-performing, you may want to use this class ONLY
    to handle migrations and combine it with hand-written code for read/write/connect.
    """

    VALID_LOADFILE_EXTENSIONS = ['.csv','.tsv','.yaml','.json','.sql'] # in order of application

    DEFAULT_PQUERY_ALIAS_SUFFIX = "pq_" # used to create method aliases for parameterized queries

    def __init__(self, 
                 db_file: str,
                 loadsources: Union[List[str],
                 Dict[str,str]] = [],
                 exclude_batches: Optional[Union[str, re.Pattern]] = None,
                 alias_pqueries: bool = False, # if True, will create a method for each pquery prefixed with "pq_"
                ):
        """
        Constructor for DbJig.
        
        :param db_file: File path to the SQLite database.
        :loadsources: List of filepaths that define the structure/content of the db.  Or else, a set of pseudo-files in a dict where key is the pseudo-filename and value is the pseudo-file content.
        :exclude_batches: very much a dev tool... causes it to skip any batch that matches the regex pattern
        
        If the SQLite file doesn't already exist, it is created by this method.
        Several "special" tables are created in the database including a 
        '_proc_defs' table and a '_exception_defs' table (see proc_defs() 
        and exception_defs() methods for explanation).
        
        If loadfiles provided, will automatically trigger self.upgrade()
        """
        #TODO: Consider figuring out how to make :memory: databases work, which is more complex than it looks
        assert db_file != ':memory:', "DbJig only works with actual file-backed databases."
        self._db_file: str = os.path.abspath(db_file)
        self._db_conn: Optional[sqlite3.Connection] = None
        self._db_conn_thread_id: Optional[int] = None
        self._params_cache: Dict[str,str] = None
        if isinstance(loadsources,str) or isinstance(loadsources,os.PathLike):
            loadsources = [loadsources]  # handle easy to make, laziness
        self._initial_load_sources = loadsources
        self._files_loaded_callbacks: List[Callable] = []
        self.last_test_exceptions = []
        self._cached_pk_tuples : Dict[str,Tuple[List[str],List[str]]] = {} # table name -> (pk_cols,non_pk_cols)
        self._cached_pqueries : Dict[str,str] = {}  # name -> sql
        self._retained_autonum: Optional[int] = None  # stores the most recently generated auto number

        # Build a reader from loadsources (supports list of paths/globs, dict, or single path)
        reader = self._build_reader(loadsources)
        has_sources = reader is not None and len(reader.batch_labels()) > 0

        if os.path.exists(self._db_file):
            self._validate_existing_database(self._db_file)
        else:
            if not has_sources:
                raise ValueError(f"Database file [{self._db_file}] does not exist and no loadfiles were found from the provided list {loadsources}.")
            self._create_new_database(self._db_file)
        
        if has_sources:
            if isinstance(exclude_batches,str):
                exclude_batches = re.compile(exclude_batches)   # hack for lazy people
            self.upgrade(loadsources, exclude_batches=exclude_batches)

        if alias_pqueries:
            self.alias_pqueries(method_name_suffux=self.__class__.DEFAULT_PQUERY_ALIAS_SUFFIX)

    def _build_reader(self, loadsources) -> Optional[DbMigrationFileReader]:
        """Build a DbMigrationFileReader from the given load sources.
        
        Accepts the same types as the constructor's loadsources parameter:
        - List of file paths/globs/directories
        - Dict mapping pseudo-filenames to content strings
        - None or empty list (returns None)
        
        Returns None if no sources are provided.
        """
        if loadsources is None:
            return None
        if isinstance(loadsources, dict):
            if not loadsources:
                return None
            return DbMigrationFileReader(loadsources, sql_dialect="sqlite")
        if isinstance(loadsources, (list, tuple)):
            if len(loadsources) == 0:
                return None
            # Convert Path objects to strings for the reader
            sources = [str(s) for s in loadsources]
            return DbMigrationFileReader(sources, sql_dialect="sqlite")
        if isinstance(loadsources, (str, os.PathLike)):
            return DbMigrationFileReader([str(loadsources)], sql_dialect="sqlite")
        return None

    @classmethod
    @contextmanager
    def temp_db(cls,loadfiles: List[str] = [], exclude_batches: Optional[Union[str,re.Pattern]] = None) -> Generator[Self, None, None]:
        """Context manager that creates a temporary database file and yields a DbJig object from that file.
           The database file is deleted when the context manager returns.
           This is designed to be a very thin passthrough to the normal constructor.
        """
        with tempfile.NamedTemporaryFile(suffix='.sqlite') as tmpfile:
            tmpfile.close()  # avoid creating the file
            def close_clean(fname,dbj):
                dbj.close_db()
                os.unlink(fname)

            try:
                is_trying = True
                dbj = cls(tmpfile.name,loadfiles,exclude_batches)
                yield dbj
                is_trying = False  
            finally:
                if is_trying:
                    try: # If we got here because of an exception, don't create more noise from closure errors
                        close_clean(tmpfile.name,dbj)
                    except Exception as e:
                        pass  
                else:
                    close_clean(tmpfile.name,dbj)

    def _make_callalble_from_pquery(self,pq_name: str) -> Callable:
        """Returns a callable that will execute the specified parameterized query."""
        #TODO: finish this
        def pquery(params: Optional[Dict[str, Any]] = {},use_col_names:bool = True, py_model: Optional[BaseModel] = None) -> List[Union[Tuple,sqlite3.Row]]:
            return self.pquery(pq_name,params,use_col_names=use_col_names, py_model=py_model)
        return pquery

    
    def upgrade(self, loadsources=None, exclude_batches: Optional[re.Pattern] = re.compile(r'^$')) -> Dict[str, List[MigrationFile]]:
        """
        Check the state of the current database, loadfiles provided, and if
        there are new, unapplied loadfile batches, apply them.  Return a dict
        containing the files loaded in order.
        
        This method also
        works to trigger the 'construction' of a brand new database.  Note that
        if any of the entries in loadfiles is a directory, it will be replaced
        with the files from tht directory that have relevant file suffixes (.csv
        , .tsv, .yaml,.json, and .sql)
        
        Loadfiles are batched together and executed based on the following 
        rules:
        * Any loadfile that has no ISO date prefixing its name is in batch 
          labeled 0000-00-00
        * Any loadfile that has an ISO date prefixing its name (e.g. YYYY-MM-DD)
          will be placed into a batch with files having the same prefix and
          labeled correspondingly 'YYYY-MM-DD'
        * Batches are loaded in order of their batch label and file basename within batch.
        * Any batch whose label was previously applied to the database will not
          be run.  This means that a newly added file using an already applied
          batch label will be ignored.
        * Data files are loaded into a table in order based on the filename (sans extension 
        and date)
        so that, for example, '2024-07-18_fruits.csv' would be loaded into a table
            named 'fruits'.  If the table already exists, data is appended to it.
        This means that you can create a table by running 0000-01-01_create_fruits.sql
        And then load the data with using the CSV file.
        * SQL files may contain multiple sql statement but separate SQL statements must
        be terminated by a semicolon as the last non-whitespace character on a line.
        * SQL files may contain "parameterized queries" that include named parameters of
        the style ':paramname1'.  Such queries MUST be preceded by one or more lines
        of comment lines starting with '--' whose first line must be a single name by
        which that parameterized query may be referred. Valid names may contain alphanumerics
        and underscores.  See the pquery() method for more details about this functionality.
        Note that parameterized queries are not actually executed or parsed during
        upgrade(), merely stored for later use.
        * Once a given batch has been loaded, files with that date prefix are ignored.
        * A small number of "internal use" tables are created wit the name pattern __dbjig*
        
          
        Within any batch, files are loaded according to these rules:
          * File basenames are alpha-sorted and "applied" in that order.
          * Data files are loaded first (tsv, csv, yaml) into a table that
            corresponds to its file name (file suffix, date prefix, trailing/leading
             underscores in base name removed)
              * Example:  2024-07-18_fruits.tsv would result in table "fruits"
              * If the table already exists (perhaps because it was added by 
                a *.sql file), contents will be appended into the existing table
              * If the table didn't exist, all columns will be of type TEXT and nullable
            
        Note: By default any imported data file that is named 'proc_defs' or 
        'exception_defs' and that has the correct column names, will be loaded into
        the database's proc_defs() or exception_defs() immediately.  If that's 
        then intent, your *.sql file can/should remove your own table as
        unnecessary.
        """
        # Build reader from provided sources or fall back to initial sources
        if loadsources is None:
            reader = self._build_reader(self._initial_load_sources)
        else:
            reader = self._build_reader(loadsources)
        
        if reader is None:
            return {}

        success_filesets = {}
        db_conn = self.db()
        if exclude_batches is None:
            exclude_batches = re.compile(r'^$')  # same as saying, "exclude nothing"
        
        applied_labels = set(self.loaded_labels())
        all_batches = reader.batch_labels()
        batches_to_load = [b for b in all_batches if b not in applied_labels and not exclude_batches.match(b)]

        if batches_to_load:
            self._cached_pk_tuples.clear()
            self._cached_pqueries.clear()
        try:
            for label in batches_to_load:
                files = reader.migration_files(batches=[label], add_transaction_files=False)
                len(self.params) # force a reload of params - was causing transaction problems for some reason
                db_conn.execute("BEGIN TRANSACTION;")
                self._apply_migration_files(files, db_conn)
                self._log_load(files, label, db_conn) 
                db_conn.execute("COMMIT;")
                success_filesets[label] = files
        except Exception as e:
            db_conn.execute("ROLLBACK;")  # filesets are applied atomically
            raise e   #re-raise unchanged for informational purposes
            
        return success_filesets
        # If it returns without exception, it succeeeded with all upgrades


    def _create_new_database(self,db_filepath) -> None:
        """Create a new SQLite database and initialize tables."""
        try:
            conn = sqlite3.connect(db_filepath)
        except Exception as e:
            sys.stderr.write(f"Failed attempting to open SQL DB at {db_filepath}\n")
            raise e
        try:
            conn.execute('PRAGMA foreign_keys = ON')  # Enable foreign key constraints
            cursor = conn.cursor()

            # Begin a transaction
            conn.execute('BEGIN')

            # Create __dbjig_params table
            # This table is used to define logs and configuration settings.
            cursor.execute('''
                CREATE TABLE __dbjig_params (
                    name VARCHAR(80),
                    value VARCHAR
                )
            ''')

            # Create __dbjig_param_queries table
            # unrelated to _dbjig_params, this table is for storing parameterized queries.
            cursor.execute('''
                CREATE TABLE __dbjig_param_queries (
                    name VARCHAR(80) UNIQUE PRIMARY KEY,
                    sql VARCHAR,
                    description VARCHAR    
                   )                    
                           ''')

            cursor.execute('''
                INSERT INTO __dbjig_params (name,value) VALUES ('proc_table','__dbjig_proc_defs')
            ''')
            cursor.execute('''
                INSERT INTO __dbjig_params (name,value) VALUES ('exception_table','__dbjig_exception_defs')
            ''')

            # Sources table holds files that were loaded.
            cursor.execute('''CREATE TABLE __dbjig_sources 
                              (filepath TEXT, basename TEXT, suffix TEXT, 
                               filesize INTEGER, last_modified TEXT, 
                               loaded_time TEXT, loaded_label TEXT)''')
                               
            # Commit the transaction
            conn.commit()
    
        except sqlite3.Error as e:
            # Rollback the transaction in case of an error
            conn.rollback()
    
            # Remove the database file if it exists
            if os.path.exists(db_filepath):
                os.remove(db_filepath)
    
            # Re-raise the exception
            raise e

    def _validate_existing_database(self,db_filepath) -> None:
        """Validate an existing dbjig SQLite file.  Incredibly minimal verification.  See exceptions for more thorough validation."""
        with sqlite3.connect(db_filepath) as conn:
            cursor = conn.cursor()
            # Check if __dbjig_params table exists and has rows
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='__dbjig_params'")
            if cursor.fetchone() is None:
                raise ValueError("__dbjig_params table does not exist in the database.")

            cursor.execute("SELECT COUNT(*) FROM __dbjig_params")
            if cursor.fetchone()[0] == 0:
                raise ValueError("No rows found in __dbjig_params table.")


    @property
    def db_file(self) -> str:
        """Returns the absolute filepath of the database file."""
        return self._db_file

    def select(self,sql: str, params: Optional[Dict[str, Any]] = {},use_col_names:bool = True,py_model: Optional[BaseModel] = None) -> list:
        """Quick and dirty way to run a  select query on the database.  If parameterized, should be named parameters.  Set named_cols to false if you want to access columns positionally"""
        return self.query(sql,params,use_col_names = use_col_names, py_model=py_model)

    def delete(self,table_name: str,where_sql: str, params: Optional[Dict[str, Any]] = {}) -> int:
        """Delete from the given table using the specified where clause in SQL syntax.  Returns count of rows deleted."""
        conn = self.db()
        cursor = conn.cursor()
        where_sql:str = where_sql.strip()
        where_clause = where_sql if where_sql.upper().startswith('WHERE ') else ('WHERE ' + where_sql)
        sql = f"DELETE FROM {table_name} {where_clause}"
        cursor.execute(sql,params)
        delcount = cursor.rowcount
        conn.commit()
        return delcount


    def _pk_cols(self,db_conn: sqlite3.Connection, db_table_name: str) -> Tuple[List[str], List[str]]:
        """
        Returns a two-entry tuple. The first entry is a list of Primary Key columns.  Second entry ist list of non-PK columns.
        
        Parameters:
        - db_conn: The database connection object.
        - db_table_name: The name of the database table.
        """
        if db_table_name in self._cached_pk_tuples:
            return self._cached_pk_tuples[db_table_name]
        cursor = db_conn.cursor()
        rf = cursor.row_factory
        cursor.row_factory = sqlite3.Row
        cursor.execute(f"PRAGMA table_info({db_table_name})")
        columns = cursor.fetchall()
        cursor.row_factory = rf

        pk_columns = [col['name'] for col in columns if col['pk'] > 0]
        non_pk_columns = [col['name'] for col in columns if col['pk'] == 0]

        pk_tuple = (pk_columns, non_pk_columns)
        self._cached_pk_tuples[db_table_name] = pk_tuple

        return pk_tuple

    def insert_row(self,table_name: str, row: Dict[str, Any], upsert=False, retain_autonum: bool = False) -> None:
        """Inserts a row into the specified table.
        
        Args:
            table_name: The name of the table to insert into.
            row: A dictionary containing the column names and values to insert.
            upsert: If True, performs an upsert operation (INSERT OR REPLACE) instead of a simple insert.
            retain_autonum: If True and the table has an auto-incrementing primary key, the generated
                          auto number will be retained and can be accessed via the retained_autonum property.
        """
        conn = self.db()
        cursor = conn.cursor()
        cols = ','.join(row.keys())
        vals = ','.join(':'+k for k in row.keys())
        sql = f"INSERT INTO {table_name} ({cols}) VALUES ({vals})"
        if upsert:
            pk_cols, non_pk_cols = self._pk_cols(conn,table_name)
            available_pk_cols = [col for col in pk_cols if col in row]
            if available_pk_cols:  # Only add ON CONFLICT clause if we have primary key columns in the data
                update_clause = ',\n    '.join(f"{col}=excluded.{col}" for col in row.keys() if col in non_pk_cols and col in row)
                sql = f"{sql} ON CONFLICT ({','.join(available_pk_cols)})\nDO UPDATE SET {update_clause}"
        return self.query(sql, row, retain_autonum=retain_autonum)

    def db(self) -> sqlite3.Connection:
        """
        Returns a database connection object that is thread-safe.
        
        The connection is cached at the instance level and tied to the thread that created it.
        If this method is called from a different thread than the one that created the connection,
        an exception will be raised. For multi-threaded applications, use the copy() method to
        create a new DbJig instance for each thread.
        
        Please note that users should call close() when they are done with the connection,
        or ensure that the DbJig object is properly garbage collected.
        """
        current_thread_id = threading.get_ident()
        
        # If we already have a connection, check if it's from the same thread
        if self._db_conn is not None:
            if self._db_conn_thread_id != current_thread_id:
                raise RuntimeError(
                    f"Attempted to access database connection from thread {current_thread_id} "
                    f"but connection was created in thread {self._db_conn_thread_id}. "
                    f"Use the copy() method to create a new DbJig instance for this thread."
                )
            return self._db_conn
        
        # Create a new connection if we don't have one
        self._db_conn = sqlite3.connect(self.db_file, isolation_level=None)
        self._db_conn_thread_id = current_thread_id
        return self._db_conn

    def close(self) -> None:
        """
        Closes the database connection if it exists and was created by the current thread.
        
        If called from a different thread than the one that created the connection,
        an exception will be raised.
        """
        if self._db_conn is not None:
            current_thread_id = threading.get_ident()
            if self._db_conn_thread_id != current_thread_id:
                raise RuntimeError(
                    f"Attempted to close database connection from thread {current_thread_id} "
                    f"but connection was created in thread {self._db_conn_thread_id}. "
                    f"Use the copy() method to create a new DbJig instance for this thread."
                )
            self._db_conn.close()
            self._db_conn = None
            self._db_conn_thread_id = None

    def __del__(self):
        """
        Destructor to ensure the database connection is closed.
        """
        if hasattr(self, '_db_conn') and self._db_conn is not None:
            try:
                # Close the connection and set it to None
                self._db_conn.close()
                self._db_conn = None
                self._db_conn_thread_id = None
            except Exception:
                pass  # Ignore errors during cleanup

    def copy(self) -> 'DbJig':
        """
        Creates a copy of this DbJig instance without a cached connection.
        
        This is the recommended way to pass a DbJig object to a different thread.
        The new instance will create its own connection when db() is called.
        """
        # Create a new instance with the same parameters
        new_instance = DbJig(
            self._db_file,
            self._initial_load_sources,
            exclude_batches=None,  # We don't need to re-run upgrades
            alias_pqueries=False   # We don't need to re-create aliases
        )
        
        # Copy over any cached data that doesn't involve connections
        new_instance._params_cache = self._params_cache
        new_instance._cached_pk_tuples = self._cached_pk_tuples.copy()
        new_instance._cached_pqueries = self._cached_pqueries.copy()
        new_instance._files_loaded_callbacks = self._files_loaded_callbacks.copy()
        
        # Explicitly set connection-related attributes to None
        new_instance._db_conn = None
        new_instance._db_conn_thread_id = None
        
        return new_instance

    def close_db(self) -> None:
        """Closes the database connection."""
        self.close()

    def alias_pqueries(self,method_name_suffix:Optional[str] = None) -> None:
        """
        Creates a set of runtime methods that are aliases for the pquery() method.  
        The method names will be the same as the pquery name but with the suffix added.
        If method_name_suffix is None, the default suffix is used.  Use an empty string if you want no suffix
        """
        for pq in self.param_queries():
            self._make_pquery_alias(pq['name'], (method_name_suffix or self.__class__.DEFAULT_PQUERY_ALIAS_SUFFIX) + pq['name'])


    def _make_pquery_alias(self,pq_name: str,meth_name:Optional[str]=None) -> Callable:
        """Returns a callable that will execute the specified parameterized query."""
        def _pquery(params: Optional[Dict[str, Any]] = {},use_col_names:bool = True, py_model: Optional[BaseModel] = None) -> List[Union[Tuple,sqlite3.Row]]:
            return self.pquery(pq_name,params,use_col_names=use_col_names, py_model=py_model)
    
        # add a new runtime method to self that calls _pquery above
        # test to confirm that the method name is not already in use
        if meth_name and not hasattr(self,meth_name):
            setattr(self,meth_name or pq_name,_pquery)
        else:
            raise ValueError(f"Method name {meth_name} is already in use or is invalid when trying to create alias for the pquery named {pq_name}.  Call this method only once.")


            
    
    def _execute_sql(self, sql: str, params: Optional[Dict[str, Any]] = {}, use_col_names: bool = True, 
                    py_model: Optional[BaseModel] = None, retain_autonum: bool = False) -> List[Union[Tuple,sqlite3.Row,BaseModel]]:
        """Internal method to execute SQL and handle common functionality like auto number retention.
           This is the shared implementation between query() and pquery().
        """
        if py_model:
            assert issubclass(py_model,BaseModel), "py_model must be a subclass of pydantic.BaseModel"
            use_col_names = True  # must use col names for pydantic models
        conn = self.db()
        old_rf,conn.row_factory = conn.row_factory,(sqlite3.Row if use_col_names else None)
        cursor = conn.cursor()
        cursor.execute(sql,params)
        conn.row_factory = old_rf
        
        # Handle auto number retention for INSERT operations
        if retain_autonum and sql.strip().upper().startswith('INSERT'):
            self._retained_autonum = cursor.lastrowid
        else:
            self._retained_autonum = None
            
        if py_model:
            return [py_model(**dict(row)) for row in cursor]
        else:
            return cursor.fetchall()

    def query(self, sql: str, params: Optional[Dict[str, Any]] = {}, use_col_names: bool = True, 
              py_model: Optional[BaseModel] = None, retain_autonum: bool = False) -> List[Union[Tuple,sqlite3.Row,BaseModel]]:
        """Executes a SQL query and returns the cursor.  Allows for named parameters of style ':paramname'  If use_col_names is True, the cursor will return rows as dictionaries.
           If a py_model is specified, the records will be mapped into the pydantic model class provided.
           
           If retain_autonum is True and the SQL command is an INSERT, the most recently generated
           auto number will be retained and can be accessed via the retained_autonum property.
        """
        return self._execute_sql(sql, params, use_col_names, py_model, retain_autonum)

    def pquery(self, pq_name: str, params: Optional[Dict[str, Any]] = {}, use_col_names: bool = True, 
               py_model: Optional[BaseModel] = None, retain_autonum: bool = False) -> List[Union[Tuple,sqlite3.Row,BaseModel]]:
        """Parameterized Queries are a crude system for DBJig to facilitate storage
           and reuse of pre-written SQL containing parameters. Parameterized queries are SQL statements
           found in imported *.sql files that satisfied certain conditions.
             
           See the param_queries() method for more detail about the conditions.
           Passing in a pydantic class as py_model will cause the result data records
           to be mapped into the pydantic model class.
           
           If retain_autonum is True and the SQL command is an INSERT, the most recently generated
           auto number will be retained and can be accessed via the retained_autonum property.
        """
        # Retrieve the SQL statement for the given proc_name
        if pq_name in self._cached_pqueries:
            sql = self._cached_pqueries[pq_name]  # definitions presumed invariant after object instantiation
        else:
            rows = self.param_queries(pq_name)
            if not rows:
                valid_pq_names = [r['name'] for r in self.param_queries()]
                raise ValueError(f"Parameterized query name not found: '{pq_name}' Valid names are: {valid_pq_names}")
            sql = rows[0]['sql']
            self._cached_pqueries[pq_name] = sql
        return self._execute_sql(sql, params, use_col_names, py_model, retain_autonum)

    def param_queries(self, target_pq_name: Optional[str]=None) -> List[sqlite3.Row]:
        """Returns a list of the names and descriptions of the parameterized queries that have been loaded.

           Paramaterized queries are loaded from *.sql files rather than executed at database
           build time.  They can be executed via the pquery() method.
           
           To be identified as a parameterized query, they must satisfy two conditions.
           First, they must be immediately preceded by one or more comment lines that start with '--' and whose first
           line is a single word that will be used to identify the query (the pq_name).  Second,
           they must contain at least one named parameter of the form ':paramname'.  If such
           a statement is found in the *.sql file, rather than being executed immediately,
           it will be stored and may be invoked via this method.

           For example, the following cluster of lines in a *.sql file would could later
           be called using a pquery("fruit_by_color",{'color':'red'})

           -- fruit_by_color
           -- This query returns all fruits of a given color.
           -- Note that it doesn't like NULLs in the color column.
              SELECT * 
              FROM fruits 
              WHERE color = :color
        """
        #TODO: add caching, possibly by using the bound_dict method
        conn = self.db()
        old_rf,conn.row_factory = conn.row_factory,sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT name,description,sql FROM __dbjig_param_queries WHERE :target IS NULL or UPPER(name) = UPPER(:target)",{'target': target_pq_name})
        rows = cursor.fetchall()
        conn.row_factory = old_rf # restore
        return rows
    
    

            
    def make_conn(self) -> sqlite3.Connection:
        """Makes a connection object, but unlike the db() method, it is not 
        used, tracked, or managed by this object after it is created.  Caller takes responsibility
        for closing/managing the connection.
        """
        return sqlite3.connect(self.db_file)

    def bound_dict(self,table_name: str, cache_expiration_seconds: int = 0) -> TableBackedDict:
        """Returns a TableBackedDict instance for the specified table.
        
        Allows access of the table via a thin passthrough that relies on the
        primary key definition of the table.  By default, no caching is used.
        """
        pk_cols, val_cols = TableBackedDict.classify_columns(self.db(),table_name)
        # Note, for below object, connection is retrieved as needed
        return TableBackedDict(table_name,self.db,pk_cols, val_cols,cache_expiration_seconds)
   
    @property
    def params(self) -> dict:
        """Returns a cached dictionary of name-value pairs from the __dbjig_params table."""
        if self._params_cache is None:
            self._params_cache = self._load_params()
        return self._params_cache

    def _load_params(self) -> dict:
        """Loads the name-value pairs from the __dbjig_params table into a dictionary."""
        cursor = self.db().cursor()
        cursor.execute("SELECT name, value FROM __dbjig_params")
        return dict(cursor.fetchall())

    def _set_param(self, name: str, value: str) -> None:
        """Upserts a parameter into the __dbjig_params table and invalidates the cache."""
        with self.db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO __dbjig_params (name, value) VALUES (?, ?) "
                           "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (name, value))
            self._params_cache = None  # Invalidate cache

    def _del_param(self, name: str) -> None:
        """Deletes a parameter from the __dbjig_params table and invalidates the cache."""
        with self.db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM __dbjig_params WHERE name = ?", (name,))
            self._params_cache = None  # Invalidate cache


    @classmethod
    def list_loadable_batches(cls, config_filepaths: List[str]) -> List[str]:
        """Returns a list of batch labels that would be loaded from the given file paths."""
        reader = DbMigrationFileReader([str(p) for p in config_filepaths], sql_dialect="sqlite")
        return reader.batch_labels()


    def apply_patch(self, file_list, db_conn: sqlite3.Connection = None, no_rollback: bool = False):
        """
        This method is a hack that forces the database to apply a specific set
        of loadfiles regardless of their naming or whether they have been
        previously applied before.  In the logs, the label for the batch will
        be made up by using system date time and the word 'PATCH' regardless
        of the names of files.  This method exists to assist developers in
        testing.
        
        No Rollback means that partially completed updates are left in place and
        is intended to assist debugging (although it does pollute the database).
        """
        db_conn = self.db()
        label = datetime.now().strftime("%Y-%m-%d_%H:%M:%S_PATCH")
        try:
            for files_group in file_list:
                reader = self._build_reader(files_group)
                if reader is None:
                    continue
                migration_files = reader.migration_files(add_transaction_files=False)
                if not no_rollback:
                    db_conn.execute("BEGIN TRANSACTION;")
                self._apply_migration_files(migration_files, db_conn)
                self._log_load(migration_files, label, db_conn)
                if not no_rollback:
                    db_conn.execute("COMMIT;")
        except Exception as e:
            if not no_rollback:
                db_conn.execute("ROLLBACK;")  # filesets are applied atomically
            raise e   #re-raise unchanged for informational purposes 

    def _apply_migration_files(self, files: List[MigrationFile], db_conn: sqlite3.Connection):
        """
        Processes a list of MigrationFile objects for data import and SQL execution.
        Delegates to DbMigrationFileReader for file parsing and SQL generation.

        For SQL files: uses iter_sql_chunks() to preserve comment metadata needed
        for parameterized query detection.
        For data files: auto-creates the target table if needed, then executes
        generated INSERT statements.

        :param files: List of MigrationFile objects to process (already sorted)
        :param db_conn: Database connection to use for operations.
        """
        for mf in files:
            if mf.info.is_sql_file:
                self._apply_sql_chunks(mf.iter_sql_chunks(), db_conn, mf.info.basename)
            elif mf.info.is_data_file:
                self._ensure_table_for_data(mf, db_conn)
                for chunk in mf.iter_sql_chunks():
                    try:
                        db_conn.execute(chunk.sql)
                    except sqlite3.Error as e:
                        raise sqlite3.Error(f"Error inserting data from '{mf.info.basename}': {e}")

    def _apply_sql_chunks(self, chunks: Iterator[SqlChunk], db_conn: sqlite3.Connection, filename: str) -> None:
        """Process SQL chunks from a .sql file, detecting and storing parameterized queries.
        
        Parameterized queries are identified by:
        1. Being preceded by comment line(s) whose first line is a single identifier name
        2. Containing at least one named parameter of the form ':paramname'
        
        Such statements are stored in __dbjig_param_queries rather than executed.
        All other SQL statements are executed directly.
        """
        chunk: SqlChunk = None
        try:
            for chunk in chunks:
                m_name = re.match(r'^\s*(--|#)\s+([a-zA-Z_]\w*)\s*$', chunk.preceding_comment[0]) if chunk.preceding_comment else None
                has_param = re.search(r'(^|\W):[a-zA-Z_]\w*\b', chunk.sql)
                if m_name and has_param:  # param query
                    pq_name = m_name.group(2)
                    pq_descr = "\n".join(re.sub(r'^\s*(--|#)\s?', '', s) for s in chunk.preceding_comment[1:])
                    # Insert or update the parameterized query in the database
                    db_conn.execute("""
                                        INSERT INTO __dbjig_param_queries (name, sql, description)
                                        VALUES (?, ?, ?)
                                        ON CONFLICT(name) DO UPDATE SET sql = excluded.sql, description = excluded.description
                                    """, (pq_name, chunk.sql, pq_descr))
                else:  # normal query, simply execute
                    db_conn.execute(chunk.sql)
        except sqlite3.ProgrammingError as e:
            start_line = chunk.start_line if chunk else 0
            raise ValueError(f"The sql statement around line {start_line} in file {filename} is not a valid parameterized query. "
                            f"It must be a single statement that contains at least one named parameter of the form ':paramname'. "
                            f"It must be preceded by one or more comment lines starting with '--' whose first line is a single name by which "
                            f"that parameterized query may be later referred. GENERATED ERROR:\n{e}")
        except sqlite3.Error as e:
            start_line = chunk.start_line if chunk else 0
            raise sqlite3.Error(f"Error in file '{filename}' statement that starts on line {start_line}: {e}")

    def _ensure_table_for_data(self, mf: MigrationFile, db_conn: sqlite3.Connection) -> None:
        """Auto-create a table for a data file if it does not already exist.
        
        Uses MigrationFile.column_names to determine the columns. All columns
        are created as untyped (TEXT in SQLite). If the table already exists,
        this method does nothing.
        """
        table_name = mf.info.entity_name
        cursor = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND UPPER(name) = UPPER(?)",
            (table_name,))
        if cursor.fetchone() is None:
            cols = mf.column_names
            if cols:
                create_sql = f"CREATE TABLE {table_name} ({', '.join(cols)})"
                db_conn.execute(create_sql)


    class LoadJournalEntry:
        def __init__(self, filepath, basename, suffix, filesize, last_modified, loaded_time, loaded_label):
            self.filepath = filepath  # relative to path of the database file
            self.basename = basename
            self.suffix = suffix
            self.filesize = filesize
            self.last_modified = last_modified
            self.loaded_time = loaded_time
            self.loaded_label = loaded_label 


    def register_files_loaded_callback(self, callback: Callable) -> None:
        """
        Whenever a fileset is loaded, callback receives a list of LoadJournalEntry objects.
        It may be important to note that because of the way transactions are structured
        there is a small possibility that a database rollback could undo the load
        reported by this method.
        """
        self._files_loaded_callbacks.append(callback)

    def _trigger_files_loaded_callback(self,journal_entries: List[LoadJournalEntry]) -> None:
        """Send a list of LoadJournalEntries, one per file loaded in a given loadset."""
        for callback in self._files_loaded_callbacks:
            callback(journal_entries)
    

    def _log_load(self, file_list: List[MigrationFile], loaded_label: str, db_conn: sqlite3.Connection) -> None:
        """
        Logs a list of MigrationFile objects to the '__dbjig_sources' table.

        :param file_list: List of MigrationFile objects to log.
        :param loaded_label: Label for the loaded files.
        """
        loaded_time = time.strftime('%Y-%m-%d %H:%M:%S')
        journal_entries = []

        for mf in file_list:
            info = mf.info
            filepath = info.path
            # Compute relative path if it's a real filesystem path
            if filepath and os.path.isabs(filepath):
                filepath = os.path.relpath(filepath, self.db_file)
            elif filepath is None:
                filepath = info.basename  # in-memory source: use basename
            
            # Get file size and last modified from filesystem when available
            if info.path and os.path.isfile(info.path):
                filesize = os.path.getsize(info.path)
                last_modified = datetime.fromtimestamp(os.path.getmtime(info.path))
            else:
                filesize = 0
                last_modified = None

            source_info = (filepath, info.basename, info.suffix, filesize, last_modified, loaded_time, loaded_label)
            db_conn.execute("INSERT INTO __dbjig_sources "
                            "(filepath, basename, suffix, filesize, last_modified, loaded_time, loaded_label) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)", source_info
                           )
            je = self.__class__.LoadJournalEntry(*source_info)
            journal_entries.append(je)
        self._trigger_files_loaded_callback(journal_entries)


    def loaded_labels(self) -> list:
        """
        Returns a sorted list of distinct 'loaded_label' values.  These are the batch labels
        that prefix the files loaded into the database (that look like an ISO date).

        Remember that files missing the ISO date prefix are in batch labeled '0000-00-00'

        :return: List of distinct 'loaded_label' values.
        """
        with self.db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT loaded_label FROM __dbjig_sources ORDER BY loaded_time")
            return [row[0] for row in cursor.fetchall()]


    def extract_to_file(self, row_src: str, output_filepath: str) -> None:
        """
        Extracts data from a table, view, or SQL query to a CSV or TSV file.

        :param row_src: Name of a table/view or a SQL SELECT statement.
        :param output_filepath: File path for the output CSV/TSV file.
        """
        # Check for file existence
        if os.path.exists(output_filepath):
            raise FileExistsError(f"Output file already exists: {output_filepath}")

        #TODO: Future direction to allow export of YAML file format
        # Check file format
        if output_filepath.endswith('.csv'):
            delimiter = ','
        elif output_filepath.endswith('.tsv'):
            delimiter = '\t'
        else:
            raise ValueError("Unsupported file format. Only .csv and .tsv are supported at this time.")

        # Determine if row_src is a table/view name or a SQL statement
        if " " not in row_src.strip():
            row_src = f"SELECT * FROM {row_src}"

        try:
            # Execute query and write to file
            with self.db() as conn, open(output_filepath, 'w', newline='') as file:
                cursor = conn.cursor()
                cursor.execute(row_src)
                writer = csv.writer(file, delimiter=delimiter)

                # Write header
                columns = [description[0] for description in cursor.description]
                writer.writerow(columns)

                # Stream rows to file
                for row in cursor:
                    writer.writerow(row)
        except sqlite3.Error as e:
            raise sqlite3.Error(f"Error while extracting data: {e}")

    @property
    def retained_autonum(self) -> Optional[int]:
        """Returns the most recently generated auto number from an INSERT operation, if any was retained.
           Raises ValueError if no auto number has been retained (either because the last operation was not
           an INSERT or because retain_autonum was not set to True).
        """
        if self._retained_autonum is None:
            raise ValueError("No auto number is currently retained. This happens either because the last SQL operation was not an INSERT statement, or because the retain_autonum parameter was not set to True when executing the query. To retain an auto number, set retain_autonum=True when calling query() or pquery() with an INSERT statement.")
        return self._retained_autonum

#
##
######----------------------- End of Class DbJig




def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="dbjig: a tool for managing database operations",
        usage="python3 -m dtfdev.dbjig [mode] [other_options] db_filepath [source_file1] [source_file2] [...]"
    )

    # Define the mode as mutually exclusive options
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument('--upgrade', action='store_true', help='Apply all loadfile batches to the database successively')
    mode_group.add_argument('--patch', action='store_true', help='Patch the database with provided files')
    mode_group.add_argument('--dry', action='store_true', help='Create and print a plan without making changes')

    # Positional arguments
    parser.add_argument('db_filepath', type=str, help='Path to the database file')
    parser.add_argument('source_files', nargs='*', type=str, help='Source files for the operation')

    return parser.parse_args()

def emit_indented_journal_entry(entries: List[DbJig.LoadJournalEntry]) -> None:
    print(f"Batch Label: {entries[0].loaded_label}")
    for je in entries:
        if je.suffix == '.sql':
            print(f"   [{je.suffix}] {je.filepath}")
        else:
            print(f"   [{je.suffix} -> {je.basename}] {je.filepath}")
        


if __name__ == "__main__":
    args = parse_args()
    if args.dry:  #TODO: implement --dry and --patch
        raise NotImplementedError("Dry run mode is not yet implemented")
    db_jig = DbJig(args.db_filepath) # don't trigger upgrade
    db_jig.register_files_loaded_callback(emit_indented_journal_entry)
    db_jig.upgrade(args.source_files)
    print("Errors Upgrading: 0")

    
