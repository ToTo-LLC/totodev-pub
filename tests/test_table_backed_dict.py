# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
import sqlite3
from totodev_pub.dbjig_support.tbdict import TableBackedDict

def test_table_backed_dict():
    # Create a SQLite in-memory database for testing
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT)')

    # Define a function to get the connection
    get_connection = lambda: conn

    # Create a TableBackedDict instance
    tb_dict = TableBackedDict('test', get_connection, ['id'], ['value'])

    # Test __setitem__ method
    tb_dict[1] = {'value': 'test1'}
    assert conn.execute('SELECT value FROM test WHERE id = 1').fetchone()[0] == 'test1'

    # Test __getitem__ method
    assert tb_dict[1] == {'id': 1, 'value': 'test1'}

    # Test __delitem__ method
    del tb_dict[1]
    assert conn.execute('SELECT value FROM test WHERE id = 1').fetchone() is None

    # Test __contains__ method
    tb_dict[1] = {'value': 'test1'}
    assert (1 in tb_dict) == True
    del tb_dict[1]
    assert (1 in tb_dict) == False