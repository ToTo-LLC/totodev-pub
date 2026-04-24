# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
import tempfile
import os
import re
from typing import Generator
from pathlib import Path
from pydantic import BaseModel
import sqlite3
from totodev_pub.dbjig import DbJig
from totodev_pub.dbjig_support.tbdict import TableBackedDict
import threading


EXAMPLES_ROOT: Path = Path(os.path.join(os.path.dirname(__file__),'dbjig_examples'))
PILE_OF_ERRORS_CONFIG_PATH = EXAMPLES_ROOT / "pile_of_errors"   

def examples_fpattern_list(example_name) -> Path:
    exdir: Path = EXAMPLES_ROOT / example_name 
    if not exdir.is_dir():
        raise ValueError(f"Specified example directory was not found: {exdir}.")
    # DbJig expects a list of path strings that can be "glob-able"
    return [str(exdir / "*")]

@pytest.fixture
def tmp_dbpath() -> Generator[str, None, None]:
    """Generate a temporary filename for a database.  Remove the file after use."""
    # Create a temporary file and cleanup after
    fd, path = tempfile.mkstemp()
    os.close(fd)  # Close the file descriptor to avoid creating the file
    os.remove(path)  # Remove the file to ensure it is not created
    with tempfile.NamedTemporaryFile(mode='w+t', delete=False) as tmpfile:
        tmpfile.close()  # avoid creating the file
        os.unlink(tmpfile.name)
        yield tmpfile.name
        if os.path.exists(tmpfile.name):
            os.unlink(tmpfile.name)
        assert not os.path.exists(tmpfile.name),f"Failed to auot-cleanup the temp db file: {tmpfile.name}"


@pytest.fixture
def foods_dbj(tmp_dbpath) -> Generator[DbJig,None,None]:
    dbj = DbJig(tmp_dbpath,[EXAMPLES_ROOT / "foods" / "*"])
    yield dbj
    dbj.close_db()

@pytest.fixture
def usage_digest_dbj(tmp_dbpath) -> Generator[DbJig,None,None]:
    dbj = DbJig(tmp_dbpath,[EXAMPLES_ROOT / "usg" / "*"])
    yield dbj
    dbj.close_db()


def test_make_dbjig(tmp_dbpath):
    example_fpattern = str(EXAMPLES_ROOT / "foods" / "*") # glob-able
    dbj = DbJig(tmp_dbpath, [example_fpattern])
    # This would the point where you do things with the database
    assert isinstance(dbj,DbJig) #trivial
    dbj.close_db()

def test_make_dbj_built_in_tempfile():
    example_fpattern = str(EXAMPLES_ROOT / "foods" / "*") # glob-able
    dbf = None
    with DbJig.temp_db([example_fpattern]) as dbj:
        assert isinstance(dbj,DbJig)
        dbf = dbj.db_file
        assert os.path.exists(dbf)
    assert not os.path.exists(dbf),f"Temp file was not cleaned up: {dbf}"
 


def test_simple_query(foods_dbj : DbJig):
    dbj = foods_dbj
    recs = dbj.select("SELECT * from fruits order by name",use_col_names=True)
    assert recs[0]['name'] == 'apple'
    assert recs[0]['color'] == 'green' # was changed by xtra_loading sql if all is well
    #print(dbj.db_file) #DIAG
    #breakpoint() #DIAG
    assert len(recs) == 4
    #bd : TableBackedDict = fruits_dbj.bound_dict("fruits")
    #assert bd[('apple')] == 'red'
    
    recs = dbj.select("SELECT name,color from fruits where color = :color",{'color': 'yellow'},use_col_names=False)
    assert len(recs) == 1
    assert recs[0][0] == 'bananna'


def test_table_backed_dict(foods_dbj : DbJig):
    """Table backed dict lets you treat a table sorta like a dict"""
    dbj : DbJig = foods_dbj
    bd : TableBackedDict = dbj.bound_dict("fruits")
    yellow_fruits = list(bd.each_where("color = 'yellow'"))
    assert len(yellow_fruits) == 1
    assert yellow_fruits[0]['name'] == 'bananna'

    assert isinstance(bd['apple'],dict)
    assert bd['apple']['color'] == 'green'

    bd['apple'] = {'color': 'red'}  # should perform an update
    recs = dbj.select("SELECT color FROM FRUITS WHERE name = 'apple'")
    assert recs[0]['color'] == 'red'

    assert len(bd) == 4
    
    dbj.delete("fruits","color = 'green'")
    assert len(bd) == 3  # should have removed apple from the table



def test_usage_digest_db(usage_digest_dbj : DbJig):
    dbj = usage_digest_dbj
    recs = dbj.select("SELECT * from usage_digest order by master_property,lease_number",use_col_names=True)
    assert len(recs) == 0


def test_query_with_params(foods_dbj : DbJig):
    dbj = foods_dbj
    recs = dbj.query("SELECT * from fruits where color = :color",{'color': 'yellow'},use_col_names=True)
    assert len(recs) == 1
    assert recs[0]['name'] == 'bananna'

def test_pquery_with_params(foods_dbj : DbJig): 
    dbj = foods_dbj
    recs = dbj.pquery("meats_matching_source",{'source': 'pig'},use_col_names=False)
    assert len(recs) == 3

def test_missing_defs():
    # Expect the below to raise a value error because no definitions are found
    with pytest.raises(ValueError):
        _ = DbJig("tmp.db",["/tmp/missing_defs/*"])
    

class FruitModel(BaseModel):
    name: str
    color: str

def test_py_model_mapping(foods_dbj : DbJig):
    dbj = foods_dbj
    results = dbj.select("SELECT * from fruits order by name",use_col_names=True,py_model = FruitModel)
    for obj in results:
        assert isinstance(obj,FruitModel)
        assert obj.name in ['apple','bananna','kiwi','orange','grape']
        assert obj.color in ['red','yellow','green','orange','purple']



def test_pile_of_errors(tmp_dbpath):
    # Every batch except 0000-00-00 has an error
    loadable_batches = list(DbJig.list_loadable_batches([PILE_OF_ERRORS_CONFIG_PATH]) )
    assert len(loadable_batches) > 1

    def make_dbjig_initial_batch():
        dbj = DbJig(str(tmp_dbpath),PILE_OF_ERRORS_CONFIG_PATH,exclude_batches=re.compile(r"(?!0000-00-00)"))
        return dbj


    dbj = make_dbjig_initial_batch()
    dbj.close_db()
    os.unlink(tmp_dbpath)

    for allow_this_one in loadable_batches[1:]:
        exclude_batches = fr'(?!{allow_this_one})'
        dbj = make_dbjig_initial_batch()
        try:
            dbj.upgrade(None,exclude_batches=re.compile(exclude_batches))
        except (RuntimeError,ValueError,sqlite3.Error) as e: # noqa: F841
            #print(f"IGNORED ERROR: {e}") #DIAG
            pass  # we expect these to fail so this is actually the good case
        else:
            assert False,f"Expected an exception for batch {allow_this_one} but didn't get one."
        dbj.close_db()
        os.unlink(tmp_dbpath)


def test_load_from_dict(tmp_dbpath):
    # Below dict values are used to construct the SQLite datase
    def_dict = {'<<not_a_file>>/0000-00-01_tbl.sql': """
                                       CREATE TABLE icons (name TEXT PRIMARY KEY, color TEXT);
                                       CREATE TABLE other (stuff TEXT);

                                       -- sample_pquery
                                       SELECT * from icons where color = :color;
                                      """,
                '<<not_a_file>>/0000-02-00_icons.json': '[{"name": "heart", "color": "red"}, {"name": "eggplant", "color": "purple"}]'
               }
    dbj = DbJig(tmp_dbpath,def_dict)
    recs = dbj.select("SELECT * from icons where name = 'heart'",use_col_names=True)
    assert len(recs) == 1
    assert recs[0]['color'] == 'red'
    recs2 = dbj.pquery("sample_pquery",{'color': 'purple'},use_col_names=True)
    assert len(recs2) == 1
    assert recs2[0]['name'] == 'eggplant'
    dbj.alias_pqueries(method_name_suffix="pq_")
    recs3 = dbj.pq_sample_pquery({'color': 'red'},use_col_names=True)
    assert len(recs3) == 1
    assert recs3[0]['name'] == 'heart'
    dbj.close_db()

def test_auto_number_retention(tmp_dbpath):
    """Test the auto number retention functionality of DbJig."""
    # Create a new DbJig instance and initialize the database
    dbj = DbJig(tmp_dbpath, {'<<not_a_file>>/0000-00-01_init.sql': ''})
    
    # Create a table with an auto-incrementing primary key
    dbj.query("""
        CREATE TABLE test_auto (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        )
    """)
    
    # Initially, retained_autonum should raise ValueError
    with pytest.raises(ValueError, match="No auto number is currently retained"):
        _ = dbj.retained_autonum
    
    # Insert a row without retention enabled
    dbj.query("INSERT INTO test_auto (name) VALUES (?)", ["test1"])
    with pytest.raises(ValueError, match="No auto number is currently retained"):
        _ = dbj.retained_autonum  # Should raise ValueError since retention not enabled
    
    # Insert a row with retention enabled
    dbj.query("INSERT INTO test_auto (name) VALUES (?)", ["test2"], retain_autonum=True)
    assert dbj.retained_autonum == 2  # Should be 2 since it's the second row
    
    # Perform a non-INSERT operation
    dbj.query("UPDATE test_auto SET name = 'test2_updated' WHERE id = 2")
    with pytest.raises(ValueError, match="No auto number is currently retained"):
        _ = dbj.retained_autonum  # Should raise ValueError for non-INSERT operations
    
    # Insert another row with retention enabled
    dbj.query("INSERT INTO test_auto (name) VALUES (?)", ["test3"], retain_autonum=True)
    assert dbj.retained_autonum == 3  # Should be 3 since it's the third row
    
    # Verify the data in the table
    results = dbj.query("SELECT * FROM test_auto ORDER BY id")
    assert len(results) == 3
    assert results[0]['name'] == 'test1'
    assert results[1]['name'] == 'test2_updated'
    assert results[2]['name'] == 'test3'
    
    dbj.close_db()

def test_auto_number_retention_with_pquery(tmp_dbpath):
    """Test auto number retention functionality with parameterized queries."""
    # Create a new DbJig instance with a parameterized query that performs an INSERT
    def_dict = {
        '<<not_a_file>>/0000-00-01_tbl.sql': """
            CREATE TABLE test_auto_pq (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            );

            -- insert_test_item
            -- Inserts a new item into test_auto_pq
            INSERT INTO test_auto_pq (name) VALUES (:name);
        """
    }
    dbj = DbJig(tmp_dbpath, def_dict)
    
    # Initially, retained_autonum should raise ValueError
    with pytest.raises(ValueError, match="No auto number is currently retained"):
        _ = dbj.retained_autonum
    
    # Insert a row using pquery without retention enabled
    dbj.pquery("insert_test_item", {"name": "test1"})
    with pytest.raises(ValueError, match="No auto number is currently retained"):
        _ = dbj.retained_autonum  # Should raise ValueError since retention not enabled
    
    # Insert a row using pquery with retention enabled
    dbj.pquery("insert_test_item", {"name": "test2"}, retain_autonum=True)
    assert dbj.retained_autonum == 2  # Should be 2 since it's the second row
    
    # Perform a non-INSERT operation using pquery
    dbj.query("UPDATE test_auto_pq SET name = 'test2_updated' WHERE id = 2")
    with pytest.raises(ValueError, match="No auto number is currently retained"):
        _ = dbj.retained_autonum  # Should raise ValueError for non-INSERT operations
    
    # Insert another row using pquery with retention enabled
    dbj.pquery("insert_test_item", {"name": "test3"}, retain_autonum=True)
    assert dbj.retained_autonum == 3  # Should be 3 since it's the third row
    
    # Verify the data in the table
    results = dbj.query("SELECT * FROM test_auto_pq ORDER BY id")
    assert len(results) == 3
    assert results[0]['name'] == 'test1'
    assert results[1]['name'] == 'test2_updated'
    assert results[2]['name'] == 'test3'
    
    # Test inserting another row and verify the auto number
    dbj.pquery("insert_test_item", {"name": "test4"}, retain_autonum=True)
    assert dbj.retained_autonum == 4
    
    # Verify the new row was inserted correctly
    results = dbj.query("SELECT * FROM test_auto_pq WHERE id = 4")
    assert len(results) == 1
    assert results[0]['name'] == 'test4'
    
    dbj.close_db()

def test_auto_number_retention_with_insert_row(tmp_dbpath):
    """Test auto number retention functionality with insert_row method."""
    # Create a new DbJig instance and initialize the database
    dbj = DbJig(tmp_dbpath, {'<<not_a_file>>/0000-00-01_init.sql': ''})
    
    # Create a table with an auto-incrementing primary key
    dbj.query("""
        CREATE TABLE test_auto_insert (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        )
    """)
    
    # Initially, retained_autonum should raise ValueError
    with pytest.raises(ValueError, match="No auto number is currently retained"):
        _ = dbj.retained_autonum
    
    # Insert a row without retention enabled
    dbj.insert_row("test_auto_insert", {"name": "test1"})
    with pytest.raises(ValueError, match="No auto number is currently retained"):
        _ = dbj.retained_autonum  # Should raise ValueError since retention not enabled
    
    # Insert a row with retention enabled
    dbj.insert_row("test_auto_insert", {"name": "test2"}, retain_autonum=True)
    assert dbj.retained_autonum == 2  # Should be 2 since it's the second row
    
    # Insert another row with retention enabled and upsert=True
    dbj.insert_row("test_auto_insert", {"name": "test3"}, upsert=True, retain_autonum=True)
    assert dbj.retained_autonum == 3  # Should be 3 since it's the third row
    
    # Verify the data in the table
    results = dbj.query("SELECT * FROM test_auto_insert ORDER BY id")
    assert len(results) == 3
    assert results[0]['name'] == 'test1'
    assert results[1]['name'] == 'test2'
    assert results[2]['name'] == 'test3'
    
    dbj.close_db()

# Threading tests for DbJig
def test_basic_connection_management(foods_dbj):
    """Test basic connection management functionality."""
    # Connection should be created when db() is called
    conn = foods_dbj.db()
    assert conn is not None
    assert foods_dbj._db_conn is not None
    assert foods_dbj._db_conn_thread_id == threading.get_ident()
    
    # Calling db() again should return the same connection
    conn2 = foods_dbj.db()
    assert conn2 is conn
    
    # Closing the connection should clear the cached connection
    foods_dbj.close()
    assert foods_dbj._db_conn is None
    assert foods_dbj._db_conn_thread_id is None
    
    # Calling db() after close should create a new connection
    conn3 = foods_dbj.db()
    assert conn3 is not None
    assert conn3 is not conn

def test_thread_safety(foods_dbj):
    """Test that connections are properly isolated between threads."""
    # Get the main thread's connection
    main_conn = foods_dbj.db()
    main_thread_id = threading.get_ident()
    
    # Create a function to be run in a separate thread
    def thread_function():
        # This should raise an exception because we're trying to access
        # a connection created in a different thread
        with pytest.raises(RuntimeError) as excinfo:
            foods_dbj.db()
        assert "Attempted to access database connection from thread" in str(excinfo.value)
        assert str(main_thread_id) in str(excinfo.value)
    
    # Run the function in a separate thread
    thread = threading.Thread(target=thread_function)
    thread.start()
    thread.join()
    
    # The main thread's connection should still be valid
    assert foods_dbj._db_conn is main_conn
    assert foods_dbj._db_conn_thread_id == main_thread_id

def test_copy_method(foods_dbj):
    """Test that the copy() method creates a thread-safe copy."""
    # Get the main thread's connection
    main_conn = foods_dbj.db()
    
    # Create a copy of the DbJig instance
    dbjig_copy = foods_dbj.copy()
    
    # The copy should not have a cached connection
    assert dbjig_copy._db_conn is None
    assert dbjig_copy._db_conn_thread_id is None
    
    # The original should still have its connection
    assert foods_dbj._db_conn is main_conn
    
    # Getting a connection from the copy should work
    copy_conn = dbjig_copy.db()
    assert copy_conn is not None
    assert copy_conn is not main_conn
    assert dbjig_copy._db_conn_thread_id == threading.get_ident()
    
    # The original should still have its connection
    assert foods_dbj._db_conn is main_conn

def test_close_from_different_thread(foods_dbj):
    """Test that close() raises an exception when called from a different thread."""
    # Get the main thread's connection
    foods_dbj.db()
    
    # Create a function to be run in a separate thread
    def thread_function():
        # This should raise an exception because we're trying to close
        # a connection created in a different thread
        with pytest.raises(RuntimeError) as excinfo:
            foods_dbj.close()
        assert "Attempted to close database connection from thread" in str(excinfo.value)
    
    # Run the function in a separate thread
    thread = threading.Thread(target=thread_function)
    thread.start()
    thread.join()
    
    # The main thread's connection should still be valid
    assert foods_dbj._db_conn is not None

def test_del_method(foods_dbj):
    """Test that the __del__ method properly closes connections."""
    # Get a connection
    conn = foods_dbj.db()
    
    # Call close() on the DbJig instance
    foods_dbj.close()
    
    # The connection should be closed and raise an error when used
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        conn.execute("SELECT 1")

def test_multiple_threads_with_copies(foods_dbj):
    """Test that multiple threads can safely use copies of a DbJig instance."""
    # Create a list to store results from threads
    results = []
    
    # Create a function to be run in separate threads
    def thread_function(dbjig_copy, thread_id):
        # Get a connection from the copy
        conn = dbjig_copy.db()
        
        # Create a table and insert some data
        conn.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO test_table (value) VALUES (?)", (f"Thread {thread_id}",))
        
        # Query the data
        cursor = conn.execute("SELECT value FROM test_table WHERE id = last_insert_rowid()")
        result = cursor.fetchone()[0]
        
        # Store the result
        results.append(result)
        
        # Close the connection
        dbjig_copy.close()
    
    # Create and start multiple threads
    threads = []
    for i in range(5):
        dbjig_copy = foods_dbj.copy()
        thread = threading.Thread(target=thread_function, args=(dbjig_copy, i))
        threads.append(thread)
        thread.start()
    
    # Wait for all threads to complete
    for thread in threads:
        thread.join()
    
    # Check that all threads were able to insert and query data
    assert len(results) == 5
    assert all(f"Thread {i}" in results for i in range(5))

def test_connection_persistence(foods_dbj):
    """Test that connections persist between calls to db()."""
    # Get a connection
    conn1 = foods_dbj.db()
    
    # Create a table
    conn1.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY, value TEXT)")
    
    # Get the connection again
    conn2 = foods_dbj.db()
    
    # Insert data using the second connection
    conn2.execute("INSERT INTO test_table (value) VALUES (?)", ("test value",))
    
    # Query the data using the first connection
    cursor = conn1.execute("SELECT value FROM test_table")
    result = cursor.fetchone()[0]
    
    # The result should be what we inserted
    assert result == "test value"
    
    # Both connections should be the same object
    assert conn1 is conn2

def test_close_and_reopen(foods_dbj):
    """Test that closing and reopening connections works correctly."""
    # Get a connection
    conn1 = foods_dbj.db()
    
    # Create a table
    conn1.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY, value TEXT)")
    
    # Close the connection
    foods_dbj.close()
    
    # Get a new connection
    conn2 = foods_dbj.db()
    
    # Insert data using the new connection
    conn2.execute("INSERT INTO test_table (value) VALUES (?)", ("test value",))
    
    # Query the data
    cursor = conn2.execute("SELECT value FROM test_table")
    result = cursor.fetchone()[0]
    
    # The result should be what we inserted
    assert result == "test value"
    
    # The connections should be different objects
    assert conn1 is not conn2


# ============================================================================
# Characterization tests: freeze current behavior before refactoring
# ============================================================================

def test_batch_loading_order(foods_dbj: DbJig):
    """Verify loaded_labels() returns batches in expected order for foods example."""
    labels = foods_dbj.loaded_labels()
    assert labels == ["0000-00-00", "0001-00-00", "0002-00-00", "0003-00-00"]


def test_pquery_redefinition(foods_dbj: DbJig):
    """Verify meats_matching_source pquery is redefined in batch 0003-00-00 with ORDER BY.
    
    The initial definition (batch 0000-00-00 via _table_defs.sql) has no ORDER BY.
    Batch 0003-00-00 redefines it with ORDER BY name. Results should come back sorted.
    """
    dbj = foods_dbj
    recs = dbj.pquery("meats_matching_source", {'source': 'pig'}, use_col_names=True)
    assert len(recs) == 3
    # Results should be sorted by name due to ORDER BY in redefined pquery
    names = [r['name'] for r in recs]
    assert names == sorted(names), f"Expected sorted names but got {names}"


def test_data_file_auto_creates_table(tmp_dbpath):
    """Data file without preceding CREATE TABLE should auto-create a table with TEXT columns."""
    def_dict = {
        '<<test>>/0000-00-00_widgets.csv': 'id,label,count\n1,Foo,10\n2,Bar,20'
    }
    dbj = DbJig(tmp_dbpath, def_dict)
    # Table should exist
    recs = dbj.select("SELECT * FROM widgets ORDER BY id")
    assert len(recs) == 2
    assert recs[0]['label'] == 'Foo'
    # All columns are TEXT (SQLite stores them as text when no type specified)
    col_info = dbj.query("PRAGMA table_info(widgets)", use_col_names=True)
    # Columns should exist
    col_names = [c['name'] for c in col_info]
    assert 'id' in col_names
    assert 'label' in col_names
    assert 'count' in col_names
    dbj.close_db()


def test_mixed_sql_and_data_in_batch(foods_dbj: DbJig):
    """Verify that SQL and data files coexist correctly within the default batch.
    
    The foods/0000-00-00 batch has _table_defs.sql (creates tables), fruits.csv,
    meats.json, nuts.csv, veggies.yaml, and xtra_loading.sql. All should load.
    """
    dbj = foods_dbj
    # Check tables created by SQL
    recs = dbj.select("SELECT count(*) as cnt FROM fruits", use_col_names=True)
    assert recs[0]['cnt'] == 4  # 3 from csv + 1 grape from xtra_loading
    
    # meats loaded from JSON
    recs = dbj.select("SELECT count(*) as cnt FROM meats", use_col_names=True)
    assert recs[0]['cnt'] > 0
    
    # nuts loaded from CSV (auto-created table, no schema in SQL)
    recs = dbj.select("SELECT * FROM nuts ORDER BY name", use_col_names=True)
    assert len(recs) == 2
    assert recs[0]['name'] == 'almond'
    
    # veggies loaded from YAML
    recs = dbj.select("SELECT count(*) as cnt FROM veggies", use_col_names=True)
    assert recs[0]['cnt'] > 0


def test_sql_file_comment_styles(tmp_dbpath):
    """Verify SQL files with # comments parse correctly."""
    def_dict = {
        '<<test>>/0000-00-00_init.sql': (
            "# This is a hash comment\n"
            "CREATE TABLE t1 (x TEXT);\n"
            "\n"
            "-- This is a dash comment\n"
            "INSERT INTO t1 VALUES ('hello');\n"
        )
    }
    dbj = DbJig(tmp_dbpath, def_dict)
    recs = dbj.select("SELECT * FROM t1", use_col_names=True)
    assert len(recs) == 1
    assert recs[0]['x'] == 'hello'
    dbj.close_db()


def test_sql_file_no_trailing_semicolon(tmp_dbpath):
    """SQL file whose last statement has no semicolon should still execute."""
    def_dict = {
        '<<test>>/0000-00-00_init.sql': (
            "CREATE TABLE t1 (x TEXT);\n"
            "INSERT INTO t1 VALUES ('no_semi')"
        )
    }
    dbj = DbJig(tmp_dbpath, def_dict)
    recs = dbj.select("SELECT * FROM t1")
    assert len(recs) == 1
    assert recs[0]['x'] == 'no_semi'
    dbj.close_db()


def test_log_load_entries(foods_dbj: DbJig):
    """Verify __dbjig_sources entries have correct basenames, suffixes, and labels."""
    dbj = foods_dbj
    recs = dbj.select(
        "SELECT basename, suffix, loaded_label FROM __dbjig_sources ORDER BY loaded_label, basename",
        use_col_names=True
    )
    assert len(recs) > 0
    
    # All records should have non-empty basename and suffix
    for r in recs:
        assert r['basename'], "basename should not be empty"
        assert r['suffix'], "suffix should not be empty"
        assert r['loaded_label'], "loaded_label should not be empty"
    
    # Check that batch labels are correct
    labels = sorted(set(r['loaded_label'] for r in recs))
    assert labels == ["0000-00-00", "0001-00-00", "0002-00-00", "0003-00-00"]
    
    # Check specific expected files
    basenames = [r['basename'] for r in recs]
    assert '_table_defs.sql' in basenames
    assert 'fruits.csv' in basenames
    assert 'meats.json' in basenames


def test_exclude_batches(tmp_dbpath):
    """Verify exclude_batches regex correctly skips specified batches."""
    def_dict = {
        '<<test>>/0000-00-00_init.sql': 'CREATE TABLE t1 (x TEXT);',
        '<<test>>/0001-00-00_data.sql': "INSERT INTO t1 VALUES ('batch1');",
        '<<test>>/0002-00-00_data.sql': "INSERT INTO t1 VALUES ('batch2');",
    }
    # Exclude batch 0001-00-00
    dbj = DbJig(tmp_dbpath, def_dict, exclude_batches=r'0001-00-00')
    recs = dbj.select("SELECT * FROM t1")
    values = [r['x'] for r in recs]
    assert 'batch1' not in values, "batch 0001-00-00 should have been excluded"
    assert 'batch2' in values, "batch 0002-00-00 should have been included"
    
    labels = dbj.loaded_labels()
    assert '0001-00-00' not in labels
    assert '0002-00-00' in labels
    dbj.close_db()