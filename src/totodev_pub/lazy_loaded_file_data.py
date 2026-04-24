# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Lazy-loaded, multi-format file data access with automatic change detection.

This module provides utilities for treating structured data files (YAML, JSON, TOML, CSV, TSV, JSONL)
as in-memory objects with minimal code overhead. It solves a common problem in data-driven
applications: managing supporting data files (lookup tables, configuration data, reference
tables) without embedding them in code or requiring a database.

Why use this
------------
- **File-based data storage**: Store supporting data in easily editable, version-controlled
  files rather than hardcoded in application code or locked in databases.
- **Zero-configuration access**: Treat data files as native Python objects with just
  one line of code - no parsing, no file handling, no error management.
- **Lazy loading**: Files are only loaded when first accessed, spreading I/O across
  program runtime rather than blocking startup.
- **Multi-format support**: Works with YAML, JSON, TOML, CSV, TSV, and JSONL/NDJSON files seamlessly.
- **Automatic change detection**: Long-running programs can automatically reload data
  when files change on disk.
- **Type safety**: Clear separation between dictionary and list data structures with
  appropriate method calls.

When this shines
----------------
This approach is ideal for applications that need to reference supporting data that
changes infrequently, for example:
- **Lookup tables**: General ledger codes, country codes, product categories
- **Configuration data**: Feature flags, environment settings, business rules
- **Reference data**: Currency rates, tax tables, validation schemas
- **Static datasets**: Test data, sample records, documentation examples

It provides a file-first approach that supports:
- **Developer productivity**: Edit data in familiar formats without code changes
- **Version control**: Track data changes alongside code changes
- **Operational flexibility**: Update data files without application restarts
- **Testing**: Easy to create test datasets and swap them in/out

Core concepts
-------------
- **Lazy loading**: Files are read only when first accessed, not at object creation
- **Format detection**: File format determined by extension (.yaml, .json, .toml, .csv, .tsv, .jsonl, .ndjson)
- **Data type detection**: Automatically distinguishes between dictionary and list data
- **Change detection**: Optional automatic reloading when files are modified
- **Flex headers**: CSV files can have title rows or comments before the actual data

Quick start
-----------
Define a lookup table as a global constant:

**Important**: Always use absolute paths or paths relative to `__file__` to avoid 
current directory dependencies that can break when the script is run from different locations.

```python
from pathlib import Path
import os
from totodev_pub.lazy_loaded_file_data import LazyLoadedFileData

# Simple lookup table - loads only when first accessed
# Option 1: Using os.path (shown in examples)
GL_CODES = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), "data", "gl_codes.yaml"))

# Option 2: Using pathlib (modern alternative)
# GL_CODES = LazyLoadedFileData(Path(__file__).parent / "data" / "gl_codes.yaml")

# Use in your code - data loads automatically on first access
def get_account_description(code: str) -> str:
    gl_data = GL_CODES.as_dict()  # Loads file on first call
    return gl_data.get(code, "Unknown account")
```

Load CSV data with automatic header detection:

```python
# CSV with title rows and comments
SALES_DATA = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), "reports", "monthly_sales.csv"), flex_header_limit=3)

def get_monthly_totals():
    data = SALES_DATA.as_list()  # Automatically skips title rows
    return sum(float(row['amount']) for row in data)
```

Configuration data with change detection:

```python
# Auto-reloads when file changes (useful for long-running services)
APP_CONFIG = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), "config", "settings.yaml"), change_detection_secs=60)

def get_feature_flag(flag_name: str) -> bool:
    config = APP_CONFIG.as_dict()
    return config.get('features', {}).get(flag_name, False)
```

Real-world examples
-------------------
**General Ledger Codes Lookup**
```yaml
# data/gl_codes.yaml
"4000": "Sales Revenue"
"4100": "Service Revenue" 
"5000": "Cost of Goods Sold"
"6000": "Operating Expenses"
```

```python
GL_CODES = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), "data", "gl_codes.yaml"))

def format_transaction(amount: float, code: str) -> str:
    description = GL_CODES.as_dict()[code]
    return f"{code} - {description}: ${amount:,.2f}"
```

**Product Catalog with Categories**
```csv
# data/products.csv
Title: Product Catalog
Generated: 2024-01-15
Notes: Updated pricing for Q1

sku,name,category,price,active
WIDGET-001,Deluxe Widget,Widgets,29.99,true
GADGET-002,Basic Gadget,Gadgets,15.50,true
TOOL-003,Pro Tool,Tools,89.99,false
```

```python
PRODUCTS = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), "data", "products.csv"), flex_header_limit=5)

def get_products_by_category(category: str):
    products = PRODUCTS.as_list()
    return [p for p in products if p['category'] == category and p['active'] == 'true']
```

**Multi-environment Configuration**
```json
{
  "database": {
    "host": "localhost",
    "port": 5432,
    "name": "myapp_dev"
  },
  "features": {
    "new_ui": true,
    "beta_features": false
  },
  "limits": {
    "max_connections": 100,
    "timeout_seconds": 30
  }
}
```

```python
CONFIG = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), "config", "development.json"))

def get_db_connection_string() -> str:
    db_config = CONFIG.as_dict()['database']
    return f"postgresql://{db_config['host']}:{db_config['port']}/{db_config['name']}"
```

**Country/Region Reference Data**
```toml
# data/countries.toml
[US]
name = "United States"
currency = "USD"
timezone = "America/New_York"
phone_prefix = "+1"

[CA]
name = "Canada" 
currency = "CAD"
timezone = "America/Toronto"
phone_prefix = "+1"

[GB]
name = "United Kingdom"
currency = "GBP"
timezone = "Europe/London"
phone_prefix = "+44"
```

```python
COUNTRIES = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), "data", "countries.toml"))

def format_phone_number(country_code: str, number: str) -> str:
    country = COUNTRIES.as_dict().get(country_code, {})
    prefix = country.get('phone_prefix', '+')
    return f"{prefix} {number}"
```

Performance and best practices
------------------------------
- **Lazy loading**: Files are only read when first accessed, spreading I/O across runtime
- **Change detection**: Use sparingly - only enable for long-running programs that need
  live data updates
- **Global constants**: Define lookup tables as module-level constants for clean access
- **Format choice**: Use YAML for human-editable config, JSON for structured data,
  CSV for tabular data, TOML for configuration files, JSONL/NDJSON for line-oriented JSON (one object per line)
- **Flex headers**: Only use for CSV files with title/comment rows - not needed for
  standard CSV files

Notes
-----
- Files are read-only by design - this class is optimized for data consumption, not modification
- Change detection adds overhead - disable for high-performance scenarios
- CSV flex headers work best when the actual header row has more columns than title rows
- YAML, JSON, and TOML support both dictionary and list data; CSV, TSV, and JSONL are always list data (use as_list())
- Empty files default to empty dictionaries or lists as appropriate

Testing
-------
The examples shown in this docstring are implemented as test cases in 
``test_lazy_loaded_file_data.py``. Look for test methods with names starting with 
``test_docstring_example_`` to see working implementations of these examples.
"""

from typing import Any, Dict, Iterator, Tuple, Callable, Optional, List, Union, Generator
import os
import time
from functools import lru_cache

# Below libraries are lazy loaded as needed
# import json
# import yaml
# import tomllib, tomli_w, toml
# import csv

def _lazy_import(module_name: str, attr_names: Tuple[str, ...], extract_single: bool = False):
    """Decorator factory for lazy module imports with caching."""
    def decorator(func: Callable[[], Any]) -> Callable[[], Any]:
        @lru_cache(maxsize=1)
        def wrapper() -> Any:
            module = __import__(module_name)
            result = tuple(getattr(module, attr) for attr in attr_names)
            return result[0] if extract_single else result
        return wrapper
    return decorator


@_lazy_import('json', ('loads', 'dumps'))
def _import_json():
    """Import JSON library and return (loads, dumps) tuple."""


@_lazy_import('yaml', ('safe_load', 'safe_dump'))
def _import_yaml():
    """Import YAML library and return (safe_load, safe_dump) tuple."""


@lru_cache(maxsize=1)
def _import_toml():
    """Import TOML libraries with fallbacks and return (loads, dumps) tuple."""
    def _toml_fallback():
        try:
            import toml  # type: ignore[import-untyped]
            return toml.loads, toml.dumps
        except ImportError:
            raise ImportError("No TOML library found. Install 'tomli' and 'tomli-w' (Python 3.11+) or 'toml'")
    
    try:
        import tomllib
        import tomli_w
        return tomllib.loads, tomli_w.dumps
    except ImportError:
        return _toml_fallback()


@_lazy_import('csv', ('reader',), extract_single=True)
def _import_csv():
    """Import CSV library and return csv.reader function."""


class LazyLoadedFileData:
    """
    A class that lazily loads structured data from a file with automatic change detection.

    This class supports multiple file formats (YAML, JSON, TOML, CSV, TSV) and delays reading the file
    until explicitly requested. The class automatically detects the actual data structure (dict or list)
    and validates method calls accordingly:
    - Use as_dict() method for dictionary data structures
    - Use as_list() method for list data structures
    The class can automatically detect when the underlying file has changed and reload it
    transparently.

    Supported file formats are determined by file extension:
    - .yaml, .yml -> YAML format (can contain dict or list data)
    - .json -> JSON format (can contain dict or list data)
    - .toml -> TOML format (typically contains dict data)
    - .csv -> CSV format (always contains list data)
    - .tsv -> TSV format (always contains list data)
    - .jsonl, .ndjson -> JSONL/NDJSON format (always contains list data, each line is a JSON object)

    The required libraries are only imported when needed (lazy loading).

    Change Detection:
    - By default, checks for file changes every 5 minutes
    - Set change_detection_secs=0 to disable automatic change detection
    - Use has_changed() method to manually check for changes
    - When changes are detected, the file is automatically reloaded
    - Files must be stable (not modified in the last MIN_STABILITY_SECS) before loading

    CSV/TSV Header Processing Rules:
    
    Standard Mode (flex_header_limit=0, default):
    - First row is treated as column headers
    - Empty column headers are ignored (columns skipped)
    
    Flex Header Mode (flex_header_limit > 0):
    - Searches the first N lines (where N = flex_header_limit) for the header row
    - Header row is determined by finding the row with the most non-blank cells (widest row)
    - If multiple rows have the same width, the earliest row is chosen
    - IMPORTANT: For flex headers to work properly, the actual header row must be the 
      widest row in the first few rows. This is designed for CSV files with title rows,
      comment lines, or explanatory text before the actual data headers.
      
      Edge cases to be aware of:
      - If all rows have the same width, the first row is chosen (may not be intended header)
      - Single-column tables are pathological - all rows have width 1, so first row wins
      - Header row should have more non-blank cells than any title/comment rows above it
    
    Column Filtering Rules (Flex Header Mode only):
    - Blank column headers are ignored (columns skipped)
    - Column headers starting with '#' are ignored (columns skipped)
    - This allows for comment columns or temporary columns to be automatically excluded
    
    Universal Rules:
    - Duplicate non-blank headers raise ValueError (prevents ambiguous data access)
    - Each data row becomes a dictionary with header names as keys
    - Missing columns in data rows are filled with None values

    Example:
        # Dictionary data (YAML/JSON/TOML with dict at top level)
        lazy_data = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), 'config.yaml'))
        data = lazy_data.as_dict()  # Loads and returns immutable dict
        mutable_data = lazy_data.as_dict(mutable=True)  # Returns mutable copy
        
        # List data (CSV/TSV, or YAML/JSON with list at top level)
        lazy_data = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), 'data.csv'))
        data = lazy_data.as_list()  # Loads and returns list of dicts
        mutable_data = lazy_data.as_list(mutable=True)  # Returns list of mutable dicts
        
        # YAML/JSON with list at top level
        lazy_list = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), 'users.yaml'))  # File contains list of user objects
        users = lazy_list.as_list()  # Returns list of user dictionaries
        
        # Disable change detection
        lazy_data = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), 'config.yaml'), change_detection_secs=0)
        
        # Flex headers for CSV with title/explanation rows
        lazy_data = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), 'data.csv'), flex_header_limit=5)
        data = lazy_data.as_list()  # Automatically detects header row within first 5 lines
        
        # Example CSV file that benefits from flex headers:
        # Title: Sales Report
        # Generated: 2024-01-01
        # 
        # name,age,city,salary    <- This row will be detected as headers (widest)
        # John,25,NYC,50000
        # Jane,30,LA,60000
        
        # Flex headers with comment columns (columns starting with '#' are ignored):
        # Title: Data Report
        # name,age,#notes,city    <- 'name', 'age', 'city' will be used as headers
        # John,25,Some note,NYC
        # Jane,30,Another note,LA
        
        # Backward compatibility mode
        lazy_data = LazyLoadedFileData(os.path.join(os.path.dirname(__file__), 'config.yaml'), acts_as_dict_proxy=True)
        print(lazy_data['key'])  # Direct dict-like access

    Note: By default, dict-like access methods will raise an AttributeError.
    Set acts_as_dict_proxy=True to enable dict-like access for backward compatibility.
    """
    
    # Minimum time a file must be stable before loading (prevents loading actively edited files)
    MIN_STABILITY_SECS = 0.1

    def __init__(self, filepath: str, acts_as_dict_proxy: bool = False, change_detection_secs: float = 5*60, flex_header_limit: int = 0, require_exists: bool = False, default_data: Union[Dict[str, Any], List[Dict[str, Any]], None] = None) -> None:
        """
        Initialize the LazyLoadedFileData with a given file path.

        :param filepath: Path to the structured data file to be lazily loaded.
                        Supported formats: YAML (.yaml, .yml), JSON (.json), TOML (.toml), CSV (.csv), TSV (.tsv)
        :param acts_as_dict_proxy: If True, enables (read-only) dict-like access methods for backward compatibility.
                                 If False (default), dict-like access will raise an exception.
                                 Use as_dict() or as_list() method to access data when this is False.
        :param change_detection_secs: Interval in seconds to check for file changes. If 0, change detection is disabled.
                                    Default is 5 minutes (300 seconds).
        :param flex_header_limit: For CSV/TSV files, number of lines to search for header row. If 0 (default),
                                flex headers are disabled and first row is treated as header. If > 0, searches
                                the first N lines for the widest row (most non-blank cells) to use as header.
                                
                                Use cases for flex headers:
                                - CSV files with title rows or explanatory text before headers
                                - Files with comment lines at the top
                                - Files where the header row is not the first row
                                
                                Example: flex_header_limit=5 will search the first 5 lines and choose the
                                row with the most non-blank cells as the header row. This is useful for
                                CSV files that start with titles, dates, or other metadata.
                                
                                Note: Only use flex headers when the header row is not the first row.
                                For standard CSV files with headers in the first row, use flex_header_limit=0.
        :param require_exists: If True, checks that the file exists at construction time and raises
                             FileNotFoundError if it doesn't. If False (default), file existence is only
                             checked when data is first accessed. Use this for fail-fast validation during
                             application startup.
        :param default_data: Default data to return if the file doesn't exist. If None (default), raises
                           FileNotFoundError when file is missing. If provided, returns this data (as immutable)
                           instead of raising an error. Useful for configuration files with sensible defaults.
                           Must be a dict or list of dicts matching the expected data structure.
                           Note: The data is deep-copied to protect against external modifications.
        """
        self._filepath: str = filepath
        self._data: Union[Dict[str, Any], List[Dict[str, Any]]] = {}
        self._loaded: bool = False
        
        # Snapshot default_data to protect against external modifications
        if default_data is not None:
            import copy
            self._default_data: Union[Dict[str, Any], List[Dict[str, Any]], None] = copy.deepcopy(default_data)
        else:
            self._default_data = None
        self._file_format: Optional[str] = None
        self._acts_as_dict_proxy: bool = acts_as_dict_proxy
        self._change_detection_secs: float = change_detection_secs
        self._flex_header_limit: int = flex_header_limit
        
        # File metadata for change detection
        self._last_file_mtime: Optional[float] = None
        self._last_file_size: Optional[int] = None
        self._last_checked_at: Optional[float] = None
        
        # CSV/TSV specific metadata
        self._headers: List[str] = []
        self._delimiter: Optional[str] = None
        
        # Dynamic data type detection
        self._data_type: Optional[str] = None  # 'dict' or 'list'
        
        # Track ignore_comments flag state for caching
        self._ignore_comments_used: Optional[bool] = None
        
        # Fail-fast existence check if requested (unless default_data is provided)
        if require_exists and not os.path.isfile(self._filepath):
            if self._default_data is None:
                raise FileNotFoundError(f"No such file: {self._filepath}")

    def _get_file_format(self) -> str:
        """
        Determine the file format based on the file extension.
        
        Returns:
            str: The file format ('yaml', 'json', 'toml', 'csv', 'tsv', or 'jsonl')
            
        Raises:
            ValueError: If the file extension is not supported
        """
        if self._file_format is not None:
            return self._file_format
            
        ext = os.path.splitext(self._filepath)[1].lower()
        
        if ext in ['.yaml', '.yml']:
            self._file_format = 'yaml'
        elif ext == '.json':
            self._file_format = 'json'
        elif ext == '.toml':
            self._file_format = 'toml'
        elif ext == '.csv':
            self._file_format = 'csv'
        elif ext == '.tsv':
            self._file_format = 'tsv'
        elif ext in ['.jsonl', '.ndjson']:
            self._file_format = 'jsonl'
        else:
            raise ValueError(f"Unsupported file format: {ext}. Supported formats: .yaml, .yml, .json, .toml, .csv, .tsv, .jsonl, .ndjson")
            
        return self._file_format

    def _detect_data_type(self, data: Union[Dict[str, Any], List[Dict[str, Any]]]) -> str:
        """
        Detect the actual data type after loading.
        
        Args:
            data: The loaded data (dict or list)
            
        Returns:
            str: 'dict' or 'list'
        """
        if isinstance(data, list):
            return 'list'
        elif isinstance(data, dict):
            return 'dict'
        else:
            # Handle edge cases - convert to dict if possible
            return 'dict'

    def _is_data_dict_type(self) -> bool:
        """
        Check if the loaded data is a dictionary type.
        
        Returns:
            bool: True if data is dict type, False if list type
        """
        return self._data_type == 'dict'

    def _is_data_list_type(self) -> bool:
        """
        Check if the loaded data is a list type.
        
        Returns:
            bool: True if data is list type, False if dict type
        """
        return self._data_type == 'list'

    def _is_list_format(self) -> bool:
        """
        Determine if the file format should be loaded as a list.
        
        Returns:
            bool: True if the format should be loaded as a list (CSV/TSV/JSONL/NDJSON), False otherwise
        """
        file_format = self._get_file_format()
        return file_format in ['csv', 'tsv', 'jsonl']

    def _is_comment_line(self, line: str) -> bool:
        """
        Check if a line is a comment line (starts with '#' after whitespace).
        
        Args:
            line: The line to check
            
        Returns:
            bool: True if the line is a comment, False otherwise
        """
        return bool(line.strip() and line.strip().startswith('#'))

    def _validate_headers(self, headers: List[str]) -> None:
        """
        Validate CSV/TSV headers according to the rules.
        
        Args:
            headers: List of header strings
            
        Raises:
            ValueError: If duplicate non-blank headers are found
        """
        # Find non-blank headers
        non_blank_headers = [h for h in headers if h and h.strip()]
        
        # Check for duplicates
        seen = set()
        duplicates = []
        for i, header in enumerate(non_blank_headers):
            if header in seen:
                duplicates.append((i, header))
            else:
                seen.add(header)
        
        if duplicates:
            duplicate_info = ', '.join([f"'{dup[1]}' at position {dup[0]}" for dup in duplicates])
            raise ValueError(f"Duplicate column headers found: {duplicate_info}")

    def _detect_header_row(self, candidate_rows: List[List[str]]) -> Tuple[int, List[str]]:
        """
        Detect the best header row from candidate rows.
        
        The header row is determined by finding the row with the most non-blank cells.
        In case of a tie, the earliest row is chosen.
        
        Args:
            candidate_rows: List of rows to analyze for header detection
            
        Returns:
            Tuple of (header_row_index, header_row_content)
            
        Raises:
            ValueError: If no candidate rows are provided
        """
        if not candidate_rows:
            raise ValueError("No candidate rows provided for header detection")
        
        best_row_idx = 0
        best_score = 0
        
        for i, row in enumerate(candidate_rows):
            # Count non-blank cells in this row
            non_blank_count = sum(1 for cell in row if cell and cell.strip())
            
            if non_blank_count > best_score:
                best_score = non_blank_count
                best_row_idx = i
        
        return best_row_idx, candidate_rows[best_row_idx]

    def _get_ignore_columns(self, header_row: List[str]) -> List[int]:
        """
        Determine which columns should be ignored based on header content.
        
        Columns are ignored if:
        - The header is blank or empty
        - The header starts with a '#' character
        
        Args:
            header_row: List of header strings
            
        Returns:
            List of column indices to ignore
        """
        ignore_columns = []
        
        for i, header in enumerate(header_row):
            if not header or not header.strip() or header.strip().startswith('#'):
                ignore_columns.append(i)
        
        return ignore_columns

    def _filter_columns(self, row: List[str], ignore_columns: List[int]) -> List[str]:
        """
        Filter out ignored columns from a row.
        
        Args:
            row: List of cell values
            ignore_columns: List of column indices to ignore
            
        Returns:
            List of cell values with ignored columns removed
        """
        return [cell for i, cell in enumerate(row) if i not in ignore_columns]

    def _create_row_retriever_generator(self, csv_reader, data_buffer: List[List[str]], ignore_columns: List[int], ignore_comments: bool = False):
        """
        Create a generator that yields rows with columns filtered out.
        
        The generator first yields rows from the data_buffer, then continues
        reading from the csv_reader. All rows have ignored columns filtered out.
        
        Args:
            csv_reader: CSV reader object for continued reading
            data_buffer: List of rows to yield first
            ignore_columns: List of column indices to ignore
            ignore_comments: If True, skip comment lines (rows starting with '#')
            
        Yields:
            List[str]: Filtered row data
        """
        def row_retriever():
            # First, yield from buffer
            for row in data_buffer:
                # Skip comment lines if ignore_comments is True
                # Check if first cell (after stripping) starts with '#'
                if ignore_comments and row and len(row) > 0:
                    first_cell = row[0].strip() if row[0] else ''
                    if first_cell.startswith('#'):
                        continue
                filtered_row = self._filter_columns(row, ignore_columns)
                yield filtered_row
            
            # Then, yield from remaining file
            try:
                for row in csv_reader:
                    # Skip comment lines if ignore_comments is True
                    # Check if first cell (after stripping) starts with '#'
                    if ignore_comments and row and len(row) > 0:
                        first_cell = row[0].strip() if row[0] else ''
                        if first_cell.startswith('#'):
                            continue
                    filtered_row = self._filter_columns(row, ignore_columns)
                    yield filtered_row
            except StopIteration:
                pass  # End of file
        
        return row_retriever()

    def _load_csv_data(self, ignore_comments: bool = False) -> List[Dict[str, Any]]:
        """
        Load CSV/TSV data as list of dictionaries with flexible header detection.
        
        Args:
            ignore_comments: If True, skip lines starting with '#' in data rows (after header detection).
        
        Returns:
            List[Dict[str, Any]]: List of dictionaries where each row is a dict with header names as keys
            
        Raises:
            ValueError: If duplicate headers are found or parsing fails
        """
        file_format = self._get_file_format()
        
        # Determine delimiter
        if file_format == 'csv':
            delimiter = ','
        elif file_format == 'tsv':
            delimiter = '\t'
        else:
            raise ValueError(f"Invalid format for CSV loading: {file_format}")
        
        self._delimiter = delimiter
        
        with open(self._filepath, 'r', encoding='utf-8', newline='') as file:
            csv_reader = _import_csv()(file, delimiter=delimiter)
            
            try:
                if self._flex_header_limit > 0:
                    # Flex headers enabled: read all rows first
                    all_rows = list(csv_reader)
                    
                    if not all_rows:
                        return []  # Empty file
                    
                    # Limit to flex_header_limit for header detection
                    candidate_rows = all_rows[:self._flex_header_limit]
                    
                    # Detect header row
                    header_row_idx, header_row = self._detect_header_row(candidate_rows)
                    
                    # Determine ignore columns
                    ignore_columns = self._get_ignore_columns(header_row)
                    
                    # Get data rows (all rows after the detected header)
                    data_rows = all_rows[header_row_idx + 1:]
                    
                    # Create row retriever generator
                    row_retriever = self._create_row_retriever_generator(
                        iter([]), data_rows, ignore_columns, ignore_comments  # Empty iterator since we have all data
                    )
                    
                else:
                    # Standard behavior: first row is header
                    header_row = next(csv_reader)
                    if not header_row:
                        return []  # Empty file
                    
                    # No ignore columns for standard mode
                    ignore_columns = []
                    
                    # Create row retriever generator with empty data buffer (header row not included)
                    row_retriever = self._create_row_retriever_generator(
                        csv_reader, [], ignore_columns, ignore_comments
                    )
                
                # Process headers: strip whitespace, handle empty headers
                headers = [h.strip() if h else '' for h in header_row]
                self._headers = headers
                
                # Validate headers (only non-ignored columns)
                non_ignored_headers = [h for i, h in enumerate(headers) if i not in ignore_columns]
                self._validate_headers(non_ignored_headers)
                
                # Create list of header names (excluding ignored columns)
                header_names = []
                for i, header in enumerate(headers):
                    if i not in ignore_columns:  # Only exclude ignored columns
                        if self._flex_header_limit > 0:
                            # In flex header mode, only include non-blank headers
                            if header and header.strip():
                                header_names.append(header.strip())
                        else:
                            # In standard mode, include non-blank headers (empty headers are ignored)
                            if header and header.strip():
                                header_names.append(header.strip())
                
                # Process data rows using the generator
                data = []
                row_num = 0  # Track row number for error reporting
                
                for row_num, filtered_row in enumerate(row_retriever, start=1):
                    if not filtered_row:  # Skip empty rows
                        continue
                    
                    # Create dictionary for this row
                    row_dict = {}
                    
                    if self._flex_header_limit > 0:
                        # In flex header mode, filtered_row and header_names correspond directly
                        for i, header_name in enumerate(header_names):
                            if i < len(filtered_row):
                                row_dict[header_name] = filtered_row[i]
                            else:
                                row_dict[header_name] = None  # Missing column
                    else:
                        # In standard mode, we need to map from the original headers
                        # filtered_row has ignored columns removed, but we need to account for empty headers
                        for i, header in enumerate(headers):
                            if i not in ignore_columns:  # Only process non-ignored columns
                                if header and header.strip():  # Only include non-empty headers
                                    if i < len(filtered_row):
                                        row_dict[header.strip()] = filtered_row[i]
                                    else:
                                        row_dict[header.strip()] = None
                    
                    data.append(row_dict)
                
                return data
                
            except StopIteration:
                # Empty file - return empty list
                return []
            except Exception as e:
                if isinstance(e, ValueError) and ("Duplicate column headers" in str(e) or "No candidate rows" in str(e)):
                    raise  # Re-raise validation errors
                else:
                    raise ValueError(f"Error parsing {file_format.upper()} file at line {row_num}: {e}")

    def _load_jsonl_data(self, ignore_comments: bool = False) -> List[Dict[str, Any]]:
        """
        Load JSONL/NDJSON data as list of dictionaries.
        
        Each line in the file is parsed as a separate JSON object.
        Empty lines and whitespace-only lines are skipped.
        
        Args:
            ignore_comments: If True, skip lines starting with '#' (after whitespace).
        
        Returns:
            List[Dict[str, Any]]: List of dictionaries parsed from each line
            
        Raises:
            ValueError: If JSON parsing fails on any line
        """
        loads_func, _ = _import_json()
        data = []
        
        with open(self._filepath, 'r', encoding='utf-8') as file:
            for line_num, line in enumerate(file, start=1):
                # Skip empty lines and whitespace-only lines
                if not line.strip():
                    continue
                
                # Skip comment lines if ignore_comments is True
                if ignore_comments and self._is_comment_line(line):
                    continue
                
                try:
                    parsed_obj = loads_func(line.strip())
                    data.append(parsed_obj)
                except Exception as e:
                    raise ValueError(f"Error parsing JSONL file at line {line_num}: {e}")
        
        return data

    def _ensure_loaded(self, ignore_comments: bool = False) -> None:
        """
        Load the structured data from the file if not already loaded.
        Supports YAML, JSON, TOML, CSV, TSV, JSONL, and NDJSON formats based on file extension.
        If file doesn't exist and default_data is provided, uses default_data instead.
        
        Args:
            ignore_comments: If True, skip lines starting with '#' for line-oriented formats.
        """
        if not self._loaded:
            if not os.path.isfile(self._filepath):
                # If file doesn't exist but we have default_data, use that
                if self._default_data is not None:
                    self._data = self._default_data
                    self._data_type = self._detect_data_type(self._data)
                    
                    # Wrap dict data in MappingProxyType for immutability
                    if self._data_type == 'dict' and isinstance(self._data, dict):
                        from types import MappingProxyType
                        self._data = MappingProxyType(self._data)
                    
                    self._loaded = True
                    return
                else:
                    raise FileNotFoundError(f"No such file: {self._filepath}")

            # Check file stability (except on first load)
            if self._last_file_mtime is not None:  # Not first load
                current_time = time.time()
                stat = os.stat(self._filepath)
                file_age = current_time - stat.st_mtime
                
                if file_age < self.MIN_STABILITY_SECS:
                    # File is too new, wait for it to stabilize
                    time.sleep(self.MIN_STABILITY_SECS - file_age)

            file_format = self._get_file_format()
            
            # Handle list formats (CSV/TSV/JSONL)
            if self._is_list_format():
                if file_format == 'jsonl':
                    self._data = self._load_jsonl_data(ignore_comments=ignore_comments)
                else:
                    self._data = self._load_csv_data(ignore_comments=ignore_comments)
                self._data_type = 'list'  # CSV/TSV/JSONL are always lists
            else:
                # Handle dict formats (YAML/JSON/TOML) - but they might contain lists
                with open(self._filepath, "r", encoding="utf-8") as file:
                    content = file.read()
                    
                    # Handle empty content
                    if not content or not content.strip():
                        self._data = {}
                        self._data_type = 'dict'
                    elif file_format == 'yaml':
                        loads_func, _ = _import_yaml()
                        loaded_data = loads_func(content)
                        self._data = loaded_data if loaded_data is not None else {}
                        self._data_type = self._detect_data_type(self._data)
                    elif file_format == 'json':
                        loads_func, _ = _import_json()
                        loaded_data = loads_func(content)
                        self._data = loaded_data if loaded_data is not None else {}
                        self._data_type = self._detect_data_type(self._data)
                    elif file_format == 'toml':
                        loads_func, _ = _import_toml()
                        loaded_data = loads_func(content)
                        self._data = loaded_data if loaded_data is not None else {}
                        self._data_type = self._detect_data_type(self._data)
                    else:
                        raise ValueError(f"Unsupported file format: {file_format}")
                
                # Wrap dict data in MappingProxyType to make it immutable
                # This ensures that dict proxy methods (__setitem__, __delitem__) will
                # raise TypeError if someone tries to modify the data
                if self._data_type == 'dict' and isinstance(self._data, dict):
                    from types import MappingProxyType
                    self._data = MappingProxyType(self._data)
            
            # Capture file metadata AFTER successful loading to avoid race conditions
            stat = os.stat(self._filepath)
            self._last_file_mtime = stat.st_mtime
            self._last_file_size = stat.st_size
                    
            self._loaded = True

    def has_changed(self) -> bool:
        """
        Check if the underlying file has been modified since last load.
        
        Returns:
            bool: True if the file has been modified (different mtime or size), False otherwise
            
        Raises:
            FileNotFoundError: If the file doesn't exist
        """
        if not os.path.isfile(self._filepath):
            raise FileNotFoundError(f"No such file: {self._filepath}")
            
        # If we haven't loaded a file yet, it hasn't "changed" - it just needs to be loaded
        if self._last_file_mtime is None or self._last_file_size is None:
            return False
            
        stat = os.stat(self._filepath)
        return (stat.st_mtime != self._last_file_mtime or 
                stat.st_size != self._last_file_size)

    def as_dict(self, mutable: bool = False) -> Dict[str, Any]:
        """
        Load the structured data from the file and return it as a dictionary.
        
        This is the preferred way to access dict-like formats (YAML, JSON, TOML). 
        The file is loaded on first call and the result is cached for subsequent calls. 
        If change_detection_secs > 0, the file will be automatically checked for changes 
        and reloaded if necessary.
        
        Args:
            mutable: If True, returns a mutable copy of the data. If False (default),
                   returns an immutable copy (frozen dict-like behavior).
        
        Returns:
            Dict[str, Any]: The loaded data as a dictionary
            
        Raises:
            FileNotFoundError: If the file doesn't exist
            ValueError: If the file format is not supported or if called on list formats
        """
        current_time = time.time()
        
        # Check if we need to check for file changes
        should_check_changes = (
            self._change_detection_secs > 0 and 
            (self._last_checked_at is None or 
             current_time - self._last_checked_at >= self._change_detection_secs)
        )
        
        if should_check_changes:
            self._last_checked_at = current_time
            
            # If file has changed, reload it (with exception handling)
            try:
                if self.has_changed():
                    self._loaded = False  # Force reload
            except (FileNotFoundError, OSError):
                # If we can't check for changes (file deleted, permission issues, etc.),
                # don't force a reload - let _ensure_loaded handle the error
                pass
        
        self._ensure_loaded()
        
        # Validate that this is a dict data type (after loading)
        if self._is_data_list_type():
            file_format = self._get_file_format()
            raise ValueError(f"This file contains list data and requires as_list() method, not as_dict(). File format: {file_format}")
        
        # At this point, _data should be a dict or MappingProxyType for dict data types
        from types import MappingProxyType
        
        # Extract the underlying dict if _data is already a MappingProxyType
        if isinstance(self._data, MappingProxyType):
            # MappingProxyType supports dict() constructor to get a copy
            data_dict = dict(self._data)
        else:
            data_dict = self._data if isinstance(self._data, dict) else {}
        
        if mutable:
            return data_dict.copy() if not isinstance(self._data, MappingProxyType) else data_dict
        else:
            # Return a truly immutable dict using types.MappingProxyType
            return MappingProxyType(data_dict)  # Truly immutable view of the dict

    def as_list(self, mutable: bool = False, ignore_comments: bool = False) -> List[Dict[str, Any]]:
        """
        Load the structured data from the file and return it as a list of dictionaries.
        
        This is the preferred way to access list-like formats (CSV, TSV). 
        The file is loaded on first call and the result is cached for subsequent calls. 
        If change_detection_secs > 0, the file will be automatically checked for changes 
        and reloaded if necessary.
        
        For large files where you don't want to load everything into memory at once,
        consider using iter_list() instead, which yields rows one at a time.
        
        Args:
            mutable: If True, returns a list of mutable dict copies. If False (default),
                   returns a list of immutable dict views.
            ignore_comments: If True, skip lines starting with '#' (after whitespace) for
                           line-oriented formats (CSV, TSV, JSONL, NDJSON). For CSV/TSV,
                           comments are only skipped after header detection. Default is False.
        
        Returns:
            List[Dict[str, Any]]: The loaded data as a list of dictionaries
            
        Raises:
            FileNotFoundError: If the file doesn't exist
            ValueError: If the file format is not supported or if called on dict formats
        """
        current_time = time.time()
        
        # Check if ignore_comments flag has changed (cache invalidation)
        if self._ignore_comments_used is not None and ignore_comments != self._ignore_comments_used:
            self._loaded = False  # Force reload with new flag
        
        # Check if we need to check for file changes
        should_check_changes = (
            self._change_detection_secs > 0 and 
            (self._last_checked_at is None or 
             current_time - self._last_checked_at >= self._change_detection_secs)
        )
        
        if should_check_changes:
            self._last_checked_at = current_time
            
            # If file has changed, reload it (with exception handling)
            try:
                if self.has_changed():
                    self._loaded = False  # Force reload
            except (FileNotFoundError, OSError):
                # If we can't check for changes (file deleted, permission issues, etc.),
                # don't force a reload - let _ensure_loaded handle the error
                pass
        
        self._ensure_loaded(ignore_comments=ignore_comments)
        
        # Update cached flag state after loading
        self._ignore_comments_used = ignore_comments
        
        # Validate that this is a list data type (after loading)
        if self._is_data_dict_type():
            file_format = self._get_file_format()
            raise ValueError(f"This file contains dictionary data and requires as_dict() method, not as_list(). File format: {file_format}")
        
        # At this point, _data should be a list for list data types
        data_list = self._data if isinstance(self._data, list) else []
        
        if mutable:
            # Return a list of mutable dict copies
            return [row.copy() for row in data_list]
        else:
            # Return a list of immutable dict views using types.MappingProxyType
            from types import MappingProxyType
            return [MappingProxyType(row) for row in data_list]

    def _iter_csv_data(self, ignore_comments: bool = False) -> Generator[Dict[str, Any], None, None]:
        """Yield CSV/TSV rows one at a time, always reading fresh from file."""
        file_format = self._get_file_format()
        
        # Determine delimiter
        if file_format == 'csv':
            delimiter = ','
        elif file_format == 'tsv':
            delimiter = '\t'
        else:
            raise ValueError(f"Invalid format for CSV iteration: {file_format}")
        
        with open(self._filepath, 'r', encoding='utf-8', newline='') as file:
            csv_reader = _import_csv()(file, delimiter=delimiter)
            
            try:
                if self._flex_header_limit > 0:
                    # Flex headers enabled: read first N rows to detect header
                    candidate_rows = []
                    for _ in range(self._flex_header_limit):
                        try:
                            candidate_rows.append(next(csv_reader))
                        except StopIteration:
                            break
                    
                    if not candidate_rows:
                        return  # Empty file
                    
                    # Detect header row
                    header_row_idx, header_row = self._detect_header_row(candidate_rows)
                    
                    # Determine ignore columns
                    ignore_columns = self._get_ignore_columns(header_row)
                    
                    # Skip rows before header, then continue from header+1
                    # We've already read candidate_rows, so we need to skip header_row_idx rows
                    # and start yielding from header_row_idx + 1
                    data_rows = candidate_rows[header_row_idx + 1:]
                    
                    # Create row retriever generator
                    row_retriever = self._create_row_retriever_generator(
                        csv_reader, data_rows, ignore_columns, ignore_comments
                    )
                    
                else:
                    # Standard behavior: first row is header
                    header_row = next(csv_reader)
                    if not header_row:
                        return  # Empty file
                    
                    # No ignore columns for standard mode
                    ignore_columns = []
                    
                    # Create row retriever generator
                    row_retriever = self._create_row_retriever_generator(
                        csv_reader, [], ignore_columns, ignore_comments
                    )
                
                # Process headers: strip whitespace, handle empty headers
                headers = [h.strip() if h else '' for h in header_row]
                
                # Validate headers (only non-ignored columns)
                non_ignored_headers = [h for i, h in enumerate(headers) if i not in ignore_columns]
                self._validate_headers(non_ignored_headers)
                
                # Create list of header names (excluding ignored columns)
                header_names = []
                for i, header in enumerate(headers):
                    if i not in ignore_columns:
                        if header and header.strip():
                            header_names.append(header.strip())
                
                # Process data rows using the generator
                row_num = 0
                
                for row_num, filtered_row in enumerate(row_retriever, start=1):
                    if not filtered_row:  # Skip empty rows
                        continue
                    
                    # Create dictionary for this row
                    row_dict = {}
                    
                    if self._flex_header_limit > 0:
                        # In flex header mode, filtered_row and header_names correspond directly
                        for i, header_name in enumerate(header_names):
                            if i < len(filtered_row):
                                row_dict[header_name] = filtered_row[i]
                            else:
                                row_dict[header_name] = None  # Missing column
                    else:
                        # In standard mode, map from original headers
                        for i, header in enumerate(headers):
                            if i not in ignore_columns:
                                if header and header.strip():
                                    if i < len(filtered_row):
                                        row_dict[header.strip()] = filtered_row[i]
                                    else:
                                        row_dict[header.strip()] = None
                    
                    yield row_dict
                    
            except StopIteration:
                # Empty file - just return (generator exhausted)
                return
            except Exception as e:
                if isinstance(e, ValueError) and ("Duplicate column headers" in str(e) or "No candidate rows" in str(e)):
                    raise  # Re-raise validation errors
                else:
                    raise ValueError(f"Error parsing {file_format.upper()} file at line {row_num}: {e}")

    def _iter_jsonl_data(self, ignore_comments: bool = False) -> Generator[Dict[str, Any], None, None]:
        """Yield JSONL/NDJSON rows one at a time, always reading fresh from file."""
        loads_func, _ = _import_json()
        
        with open(self._filepath, 'r', encoding='utf-8') as file:
            for line_num, line in enumerate(file, start=1):
                # Skip empty lines and whitespace-only lines
                if not line.strip():
                    continue
                
                # Skip comment lines if ignore_comments is True
                if ignore_comments and self._is_comment_line(line):
                    continue
                
                try:
                    parsed_obj = loads_func(line.strip())
                    yield parsed_obj
                except Exception as e:
                    raise ValueError(f"Error parsing JSONL file at line {line_num}: {e}")

    def iter_list(self, mutable: bool = False, ignore_comments: bool = False) -> Iterator[Dict[str, Any]]:
        """
        Yield rows one at a time instead of loading everything into memory.
        
        Always reads fresh from the file (ignores caching and change detection).
        This is useful for large files where you want to process rows incrementally.
        
        Args:
            mutable: If True, yields mutable dict copies. If False (default),
                   yields immutable dict views.
            ignore_comments: If True, skip lines starting with '#' (after whitespace) for
                           line-oriented formats (CSV, TSV, JSONL, NDJSON). For CSV/TSV,
                           comments are only skipped after header detection. Default is False.
        
        Returns:
            Iterator[Dict[str, Any]]: Generator that yields dictionaries one at a time
            
        Raises:
            FileNotFoundError: If the file doesn't exist
            ValueError: If the file format is not supported or if called on dict formats
        """
        # Check file exists
        if not os.path.isfile(self._filepath):
            if self._default_data is not None:
                # For default_data, we can yield from it if it's a list
                if isinstance(self._default_data, list):
                    from types import MappingProxyType
                    for row in self._default_data:
                        if mutable:
                            yield row.copy() if isinstance(row, dict) else row
                        else:
                            yield MappingProxyType(row) if isinstance(row, dict) else row
                    return
            raise FileNotFoundError(f"No such file: {self._filepath}")
        
        file_format = self._get_file_format()
        
        # Handle list formats (CSV/TSV/JSONL)
        if self._is_list_format():
            if file_format == 'jsonl':
                for row_dict in self._iter_jsonl_data(ignore_comments=ignore_comments):
                    if mutable:
                        yield row_dict.copy() if isinstance(row_dict, dict) else row_dict
                    else:
                        from types import MappingProxyType
                        yield MappingProxyType(row_dict) if isinstance(row_dict, dict) else row_dict
            else:
                for row_dict in self._iter_csv_data(ignore_comments=ignore_comments):
                    if mutable:
                        yield row_dict.copy()
                    else:
                        from types import MappingProxyType
                        yield MappingProxyType(row_dict)
            return
        
        # Handle dict formats (YAML/JSON/TOML) - but they might contain lists
        with open(self._filepath, "r", encoding="utf-8") as file:
            content = file.read()
            
            # Handle empty content
            if not content or not content.strip():
                return  # Empty file, nothing to yield
            
            loaded_data = None
            if file_format == 'yaml':
                loads_func, _ = _import_yaml()
                loaded_data = loads_func(content)
            elif file_format == 'json':
                loads_func, _ = _import_json()
                loaded_data = loads_func(content)
            elif file_format == 'toml':
                loads_func, _ = _import_toml()
                loaded_data = loads_func(content)
            else:
                raise ValueError(f"Unsupported file format: {file_format}")
            
            # Validate that this is a list data type
            if not isinstance(loaded_data, list):
                raise ValueError(f"This file contains dictionary data and requires as_dict() method, not iter_list(). File format: {file_format}")
            
            # Yield items from the list
            from types import MappingProxyType
            for item in loaded_data:
                if isinstance(item, dict):
                    if mutable:
                        yield item.copy()
                    else:
                        yield MappingProxyType(item)
                else:
                    yield item

    def get_filepath(self) -> str:
        """
        Get the file path associated with this LazyLoadedFileData.
        
        Returns:
            str: The file path
        """
        return self._filepath

    def is_loaded(self) -> bool:
        """
        Check if the data has been loaded from the file.
        
        Returns:
            bool: True if data has been loaded, False otherwise
        """
        return self._loaded

    def get_file_format(self) -> str:
        """
        Get the detected file format based on the file extension.
        
        Returns:
            str: The file format ('yaml', 'json', 'toml', 'csv', or 'tsv')
            
        Raises:
            ValueError: If the file extension is not supported
        """
        return self._get_file_format()

    def file_exists(self) -> bool:
        """
        Check if the underlying file exists.
        
        This method allows users to check for file existence without triggering
        data loading or catching exceptions. It's useful for:
        - Checking optional configuration files before attempting to load
        - Validating file paths in error handling logic
        - Implementing graceful fallback behavior when files may not exist
        
        Returns:
            bool: True if the file exists, False otherwise
            
        Example:
            config = LazyLoadedFileData("config.yaml")
            if config.file_exists():
                data = config.as_dict()
            else:
                # Use defaults or create the file
                data = {}
        """
        return os.path.isfile(self._filepath)

    @staticmethod
    def overwrite_data_file(
        data: Union[Dict[str, Any], List[Dict[str, Any]]], 
        filepath: str,
        atomic: bool = True
    ) -> None:
        """
        Write structured data to a file, inferring format from extension.
        
        This is a utility function for writing structured data files.
        It does NOT provide locking, transactions, or concurrent access protection.
        For production use cases requiring those features, implement custom logic.
        
        Supported formats:
        - YAML (.yaml, .yml): dict or list
        - JSON (.json): dict or list
        
        NOT SUPPORTED (raises ValueError):
        - TOML (.toml): Cannot ensure idempotent writes (comments lost)
        - CSV/TSV (.csv, .tsv): Use Python's csv module directly (complex structure)
        
        IMPORTANT Limitations:
        - YAML: Comments and formatting from original files are NOT preserved.
                Output will be clean YAML without comments.
                Successive writes ARE idempotent (same output each time).
                Use ruamel.yaml directly if comment preservation is critical.
        
        - JSON: Fully idempotent with consistent formatting (indent=2, sorted keys).
                Best choice for programmatic configuration management.
        
        - Atomic writes: By default, writes to a temporary file first, then atomically
                        renames to target. This prevents corruption if write fails.
        
        Args:
            data: Dictionary or list of dictionaries to write
            filepath: Target file path (format inferred from extension)
            atomic: If True (default), uses atomic write (temp file + rename)
                   to prevent corruption on write failure
        
        Raises:
            ValueError: If format is unsupported (TOML, CSV, TSV) or if data structure
                       doesn't match format expectations
            TypeError: If data is not a dict or list
            OSError: If file cannot be written
            ImportError: If required library (yaml) is not installed
            
        Example:
            # Write YAML config
            config = {'database': {'host': 'localhost', 'port': 5432}}
            LazyLoadedFileData.overwrite_data_file(config, 'config.yaml')
            
            # Write JSON data
            users = [{'name': 'Alice', 'age': 30}, {'name': 'Bob', 'age': 25}]
            LazyLoadedFileData.overwrite_data_file(users, 'users.json')
            
            # Atomic write can be disabled if needed
            LazyLoadedFileData.overwrite_data_file(config, 'config.yaml', atomic=False)
        """
        # Validate data type
        if not isinstance(data, (dict, list)):
            raise TypeError(f"Data must be dict or list, got {type(data).__name__}")
        
        # Determine file format from extension
        ext = os.path.splitext(filepath)[1].lower()
        
        # Check for unsupported formats
        if ext == '.toml':
            raise ValueError(
                "TOML format is not supported for writing. "
                "Comment preservation cannot be guaranteed. "
                "Use toml library directly if TOML output is required."
            )
        elif ext in ['.csv', '.tsv']:
            raise ValueError(
                f"{ext.upper()[1:]} format is not supported for writing. "
                "The complex header detection and column filtering used for reading "
                "cannot be reliably reversed. Use Python's csv module directly."
            )
        elif ext not in ['.yaml', '.yml', '.json']:
            raise ValueError(
                f"Unsupported file format: {ext}. "
                f"Supported formats for writing: .yaml, .yml, .json"
            )
        
        # Serialize data based on format
        if ext in ['.yaml', '.yml']:
            _, dumps_func = _import_yaml()
            content = dumps_func(data)
        elif ext == '.json':
            _, dumps_func = _import_json()
            # Use consistent formatting for idempotency
            content = dumps_func(data, indent=2, sort_keys=True, ensure_ascii=False)
            # Ensure trailing newline for POSIX compliance
            if not content.endswith('\n'):
                content += '\n'
        
        # Write to file
        if atomic:
            # Atomic write: write to temp file, then rename
            import tempfile
            import time
            
            # Create temp file name with clear indication it's temporary and can be deleted
            # Format: filename.DELETETHIS_TEMP_1130pm (time only, no date - these exist for fractions of a second)
            dir_name = os.path.dirname(filepath) or '.'
            base_name = os.path.basename(filepath)
            timestamp = time.strftime('%I%M%p').lower()  # 12-hour format with am/pm
            temp_name = f"{base_name}.DELETETHIS_TEMP_{timestamp}"
            temp_path = os.path.join(dir_name, temp_name)
            
            try:
                # Write to temp file
                with open(temp_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                
                # Atomic rename (on most OS)
                os.replace(temp_path, filepath)
            except Exception:
                # Clean up temp file if it exists
                if os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass  # Best effort cleanup
                raise
        else:
            # Direct write (non-atomic)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)

    def overwrite_source_file(self, data: Union[Dict[str, Any], List[Dict[str, Any]]] = None) -> None:
        """
        Convenience method to overwrite the source file with new data.
        
        **WARNING**: This method provides minimal safety protections:
        - No file locking (concurrent access not handled)
        - No backup creation before overwrite
        - No merge/update semantics (full overwrite only)
        - YAML/TOML comments and formatting will be lost
        
        For production use requiring safety features, use overwrite_data_file()
        with your own locking/backup logic, or implement custom persistence.
        
        Args:
            data: Data to write. If None, writes current loaded data
                 (useful for round-trip after modifications).
                 Must match the expected format for the file type.
        
        Raises:
            ValueError: If data is None and file hasn't been loaded yet,
                       or if file format is not supported for writing
            (same exceptions as overwrite_data_file)
            
        Side effects:
            - Overwrites the source file
            - Invalidates internal cache (forces reload on next access)
            - File modification time changes
            
        Example:
            # Modify and write back
            config = LazyLoadedFileData('settings.yaml')
            data = config.as_dict(mutable=True)
            data['new_setting'] = 'value'
            config.overwrite_source_file(data)  # Overwrites settings.yaml
            
            # Round-trip: read, modify, write
            users = LazyLoadedFileData('users.json')
            user_list = users.as_list(mutable=True)
            user_list.append({'name': 'Charlie', 'age': 35})
            users.overwrite_source_file(user_list)
        """
        if data is None:
            if not self._loaded:
                raise ValueError(
                    "Cannot write back unloaded data. "
                    "Load file first or provide data explicitly."
                )
            # If _data is a MappingProxyType, convert it to a regular dict
            from types import MappingProxyType
            if isinstance(self._data, MappingProxyType):
                data = dict(self._data)
            else:
                data = self._data
        
        # Write using static method
        self.overwrite_data_file(data, self._filepath)
        
        # Invalidate cache to force reload on next access
        self._loaded = False
        self._last_file_mtime = None
        self._last_file_size = None

    def _check_dict_proxy_allowed(self, method_name: str) -> None:
        """
        Check if dict-like access is allowed and raise an exception if not.
        
        :param method_name: Name of the method being called for error message
        :raises AttributeError: If acts_as_dict_proxy is False
        """
        if not self._acts_as_dict_proxy:
            raise AttributeError(
                f"Dict-like access via '{method_name}' is not enabled. "
                f"Use as_dict() method to access the data, or set acts_as_dict_proxy=True "
                f"in the constructor to enable dict-like access for backward compatibility."
            )

    def __getitem__(self, key: str) -> Any:
        """
        Get item by key (dict-like access).
        
        .. deprecated:: 
            This method is deprecated and should not be used in new code.
            Use as_dict() method instead for accessing dictionary data.
            This method is only available when acts_as_dict_proxy=True.
        """
        self._check_dict_proxy_allowed("__getitem__")
        self._ensure_loaded()
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """
        Set item by key (dict-like access).
        
        .. deprecated:: 
            This method is deprecated and should not be used in new code.
            Use as_dict(mutable=True) method instead for modifying dictionary data.
            This method is only available when acts_as_dict_proxy=True.
        """
        self._check_dict_proxy_allowed("__setitem__")
        self._ensure_loaded()
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        """
        Delete item by key (dict-like access).
        
        .. deprecated:: 
            This method is deprecated and should not be used in new code.
            Use as_dict(mutable=True) method instead for modifying dictionary data.
            This method is only available when acts_as_dict_proxy=True.
        """
        self._check_dict_proxy_allowed("__delitem__")
        self._ensure_loaded()
        del self._data[key]

    def __iter__(self) -> Iterator[str]:
        """
        Iterate over keys (dict-like access).
        
        .. deprecated:: 
            This method is deprecated and should not be used in new code.
            Use as_dict().keys() instead for iterating over dictionary keys.
            This method is only available when acts_as_dict_proxy=True.
        """
        self._check_dict_proxy_allowed("__iter__")
        self._ensure_loaded()
        return iter(self._data)

    def __len__(self) -> int:
        """
        Get length (dict-like access).
        
        .. deprecated:: 
            This method is deprecated and should not be used in new code.
            Use len(as_dict()) instead for getting dictionary length.
            This method is only available when acts_as_dict_proxy=True.
        """
        self._check_dict_proxy_allowed("__len__")
        self._ensure_loaded()
        return len(self._data)

    def keys(self) -> Iterator[str]:
        """
        Get dictionary keys (dict-like access).
        
        .. deprecated:: 
            This method is deprecated and should not be used in new code.
            Use as_dict().keys() instead for accessing dictionary keys.
            This method is only available when acts_as_dict_proxy=True.
        """
        self._check_dict_proxy_allowed("keys")
        self._ensure_loaded()
        return self._data.keys()

    def values(self) -> Iterator[Any]:
        """
        Get dictionary values (dict-like access).
        
        .. deprecated:: 
            This method is deprecated and should not be used in new code.
            Use as_dict().values() instead for accessing dictionary values.
            This method is only available when acts_as_dict_proxy=True.
        """
        self._check_dict_proxy_allowed("values")
        self._ensure_loaded()
        return self._data.values()

    def items(self) -> Iterator[Tuple[str, Any]]:
        """
        Get dictionary items (dict-like access).
        
        .. deprecated:: 
            This method is deprecated and should not be used in new code.
            Use as_dict().items() instead for accessing dictionary items.
            This method is only available when acts_as_dict_proxy=True.
        """
        self._check_dict_proxy_allowed("items")
        self._ensure_loaded()
        return self._data.items()

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get item by key with default value (dict-like access).
        
        .. deprecated:: 
            This method is deprecated and should not be used in new code.
            Use as_dict().get() instead for accessing dictionary values.
            This method is only available when acts_as_dict_proxy=True.
        """
        self._check_dict_proxy_allowed("get")
        self._ensure_loaded()
        return self._data.get(key, default)
