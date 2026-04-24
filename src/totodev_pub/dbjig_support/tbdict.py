# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub



import sqlite3
from collections.abc import Sequence
import re
from pydantic import BaseModel
from typing import Any, Callable, Dict, Generator, List, Tuple, Union,Optional
from totodev_pub.forgetful_reader import ForgetfulReader


class CacheMissForTableBackedDict:
    """Internal use class used to indicate a cache miss."""
    pass

class TableBackedDict:
    """
    Class creates a "dict-like" object that is bound to a SQLite database table.
    It performs SQL operations relatively naively on the underlying table in 
    response to operations on the
    dict.  For example, __set__ or __del__ will modify the underlying table.
    __get__ will retrieve a row from the underlying table.  The object assumes
    table structure doesn't change after its creation.
    
    It does no transaction management and does not, itself, retain a connection
    to the database.  Note that this class is built for convenience, not safety
    or efficiency.  If you need to do high-volume or transactions, you should
    use the underlying database connection directly or write custom queries.
    """
    def __init__(self, table_name: str, get_connection: Callable[[], sqlite3.Connection],
                 pk_col_names: List[str], non_pk_col_names: List[str], 
                 cache_expiration_seconds: int = 0,
                 py_model: Optional[BaseModel] = None
                ):
        """
        Reads and upserts to the given table name.  Before each interaction
        with the database, retrieves a connection with get_connection,
        the 'key' of this dict-like object is determined by the pk_col_names
        and the value by the non_pk_col_names.  Order of column names is 
        important and alphabetical is recommended unless there are good reasons
        to do otherwise.

        If provided a pydantic class as py_model, will map and return that class
        rather than than a dict for each row.
        
        Note: If you want to implement caching, create a factory for these
        objects that caches the column info and conn.
        """
        if not callable(get_connection):
            raise ValueError("get_connection must be callable, instead received a " + str(type(get_connection)))
        self.get_connection = get_connection
        self.table_name = table_name
        self.key_columns: List[str] = pk_col_names
        self.value_columns: List[str] = non_pk_col_names
        self.py_model = py_model
        self._gen_base_queries()

        # Setting below cache to zero seconds disables caching
        # Below is not merely a cache but also fetches on cache miss
        self._cache_obj = ForgetfulReader(self._fetch_row_as_dict, cache_expiration_seconds) 

    @classmethod
    def make_simple(cls,sqlite_filename: str,backing_table_name: str):
            """
            Creates a TableBackedDict instance which opens a connection for each
            operation (no connection caching)

            Parameters:
            - sqlite_filename (str): The filename of the SQLite database.
            - backing_table_name (str): The name of the table in the database.

            Returns:
            - TableBackedDict: An instance of TableBackedDict.

            """
            def connect() -> sqlite3.Connection:
                return sqlite3.connect(sqlite_filename)
            pk_cols,val_cols = TableBackedDict.classify_columns(connect(),backing_table_name)
            return TableBackedDict(backing_table_name,connect,pk_cols,val_cols)
        
    def _gen_base_queries(self) -> Tuple[str,str,str]:
        """Generates the SQL for the various operations.  The SQL is generated
        based on the column names provided in the constructor.  The SQL is
        generated once and then cached for future use."""
        
        # Note that the normalized key values (_norm_key*()) are a tuple and
        # normalized values ( _norm_value()) are a dict
        # Below parameterized queries will require a tuple of values
        self._select_by_key = f"SELECT {','.join(self.key_columns+self.value_columns)} FROM {self.table_name} WHERE {' AND '.join([f'{col}=?' for col in self.key_columns])}"
        self._delete_by_key = f"DELETE FROM {self.table_name} WHERE {','.join([f'{col}=?' for col in self.key_columns])}"
        # Below parameterized queries will require a dict of values
        self._upsert_by_key = f"INSERT OR REPLACE INTO {self.table_name} ({','.join(self.key_columns+self.value_columns)}) VALUES ({','.join([':' + col for col in self.key_columns+self.value_columns])})"
        self._select_all = f"SELECT {','.join(self.key_columns+self.value_columns)} FROM {self.table_name} "
        self._record_count = f"SELECT COUNT(*) FROM {self.table_name} "
 
    @staticmethod
    def classify_columns(db_conn: sqlite3.Connection, db_table_name: str) -> Tuple[List[str], List[str]]:
        """
        Returns a two-entry tuple. The first entry is a list of Primary Key columns.  Second entry ist list of non-PK columns.
        
        Parameters:
        - db_conn: The database connection object.
        - db_table_name: The name of the database table.
        """
        cursor = db_conn.execute(f"PRAGMA table_info({db_table_name})")
        columns = cursor.fetchall()

        pk_columns = [col[1] for col in columns if col[5] > 0]
        non_pk_columns = [col[1] for col in columns if col[5] == 0]

        return (pk_columns,non_pk_columns)

    def _norm_key(self,key: Any) -> Sequence[Any]:
        """Fix for single-element keys and lazy developers.
        This packages a single-element key into a tuple.
        """
        received_key = key
        # This is incredibly tolerant of users at the risk of hard-to-find bugs
        if isinstance(key,str) or isinstance(key,int) or isinstance(key,float):
            k = (key,)
        elif isinstance(key,dict):
            missing = [k for k in self.key_columns if k not in key.keys()]
            if len(missing) > 0:
                raise ValueError(f"Missing key columns: {missing}")
            k = tuple([key[colname] for colname in self.key_columns])
        else:   # assume it's a sequence
            k = tuple(key)
        if len(k) != len(self.key_columns):
            raise ValueError(f"Key length mismatch.  Expected tuple for {self.key_columns} but received {received_key}")
        return k

    def _norm_value(self,key,value) -> Dict[str,Any]:
        """Fix for weird values and lazy developers.
        This handles several cases:
           * dict that lacks the key fields -> gets key fields added
           * array presumed to be values -> converted to dict
           * single value -> converted to dict with first value column name
        """
        if isinstance(value,dict):
            pass
        elif isinstance(value,Sequence):
            value = dict(zip(self.value_columns,value))
        elif isinstance(value,str) or isinstance(value,int) or isinstance(value,float):
            value = {self.value_columns[0]: value}
        # Update or add the keys
        for i,colname in enumerate(self.key_columns):
            value[colname] = key[i]  # override
        missing = [k for k in (self.key_columns+self.value_columns) if k not in value.keys()]
        if len(missing) > 0:
            raise ValueError(f"Missing value columns: {missing}")
        return value

    def _sql_exec_w_rich_err_msg(self,db_conn,sql: str, params: Dict[str, Any]) -> Any:
        try:
            return db_conn.execute(sql,params)
        # Below I want to gradually build out so that catching SQL problems is easier
        except sqlite3.ProgrammingError as e:
            if "did not supply a value for binding" in e.message:
                # examine sql and make a list of all named parameters in it and cross check against params members
                # if any are missing, raise a more informative error
                expected_params = re.findall(r":([a-zA-Z0-9_]+)",sql)
                missing_params = [p for p in expected_params if p not in params]
                if len(missing_params) > 0:
                    raise ValueError(f"Missing parameter to query: {missing_params}")
            raise e  # if we fell through, just raise the original exception

    
    def __setitem__(self, key: Sequence[Any], value: Union[Dict[str, Any], BaseModel,Any]) -> None:
        """Implementation of the set method"""
        if isinstance(value,BaseModel):
            value = value.model_dump() # convert to dict
        key =self._norm_key(key)
        value = self._norm_value(key,value)
        db_conn = self.get_connection()
        self._sql_exec_w_rich_err_msg(db_conn,self._upsert_by_key, value)
        db_conn.commit() #necessary?
        self._cache_obj.override(key, value)

    def __delitem__(self, key: Sequence[Any]) -> None:
        """Implementation of the delete method"""
        key =self._norm_key(key)
        self._cache_obj.expire(key)
        db_conn = self.get_connection()
        db_conn.execute(self._delete_by_key, key)
        db_conn.commit()

    def __len__(self) -> int:
        """Implementation of the len method"""
        db_conn = self.get_connection()
        cursor = db_conn.execute(self._record_count)
        return cursor.fetchone()[0]
    
    def __iter__(self) -> Generator[Dict[str, Any], None, None]:
        """Implementation of the iter method"""
        db_conn = self.get_connection()
        cursor = db_conn.execute(self._select_all)
        for row in cursor:
            yield dict(zip(self.key_columns+self.value_columns, row))

    def each_where(self,where_clause: str) -> Generator[Dict[str, Any], None, None]:
        """Iterate over all rows in the table that match the where clause (in SQL format)"""
        #if the where clause starts with WHERE, remove it using a regular expression
        where_clause = re.sub(r"^\s*WHERE\s+","",where_clause,re.IGNORECASE)
        db_conn = self.get_connection()
        cursor = db_conn.execute(f"SELECT * FROM ({self._select_all}) WHERE {where_clause}")
        for row in cursor:
            yield dict(zip(self.key_columns+self.value_columns, row))

    def __contains__(self, key: Sequence[Any]) -> bool:
        """Implementation of the in method"""
        key = self._norm_key(key)
        result = self._cache_obj.get(key)
        return (result is not None)
    
    def __repr__(self) -> str:
        """Implementation of the repr method"""
        return f"TableBackedDict({self.table_name}, {self.key_columns}, {self.value_columns})"
    
    def __str__(self) -> str:
        """Implementation of the str method"""
        return f"TableBackedDict('{self.table_name}', {self.key_columns}, {self.value_columns})"

    def _fetch_row(self, key: Sequence[Any]) -> Union[Dict[str, Any], Any]:
        """Retrieve the corresponding row from the database.  Returns None if the key is not found."""
        key = self._norm_key(key)
        db_conn = self.get_connection()
        cursor = self._sql_exec_w_rich_err_msg(db_conn,self._select_by_key, key)
        row = cursor.fetchone()
        return row

    def _fetch_row_as_dict(self,key: Sequence[Any]) -> Union[Dict[str, Any], Any]:
        """Retrieve the corresponding row from the database.  Returns None if the key is not found."""
        row = self._fetch_row(key)
        if row is None:
            return None
        return dict(zip(self.key_columns+self.value_columns, row))

    
    def __getitem__(self, key: Sequence[Any]) -> Union[Dict[str, Any], Any]:
        """Passthrough to get method"""
        return self.get(key)



    def get(self, key: Sequence[Any], fallback: Any = CacheMissForTableBackedDict() ) -> Union[Dict[str, Any], BaseModel,Any]:
        """Retrieve corresponding record from the cache or database.  
        If the record is not found, return the fallback value.
        
        Note: This implementation may use a cache to avoid hitting the database.
        """
        key = self._norm_key(key)
        result = self._cache_obj.get(key)
        if result is None and isinstance(fallback, CacheMissForTableBackedDict):
            raise KeyError(key) # key not found in this case
        retval = fallback if result is None else result
        if self.py_model is None:
            return retval
        else:
            return self.py_model(**retval)

