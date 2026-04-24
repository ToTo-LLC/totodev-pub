# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Date-oriented, human-readable workspace directories for low-to-medium volume systems.

This module provides utilities for organizing per-day folders under a given root
using a familiar strftime-like pattern. It solves a common operational problem in
batch/worker-style systems: creating many temporary or intermediate directories
that are easy for humans to navigate, debug, and clean up, without needing a
database or service to track them.

Why use this
------------
- Human-readable directory names: Avoids opaque temp directory names and instead
  uses date folders like ``.../2024/01/15`` or ``.../2024-01-15``.
- Chronological sorting: Choosing patterns such as ``"%Y/%m/%d"`` allows plain
  alphabetical sorting to match chronological order.
- Operational clarity: Makes it easy to find the most recent workspaces, scan
  for activity on a given date, and investigate logs or artifacts by time.
- Simple retention: Built-in age-based purge keeps older directories in check
  without external schedulers or databases.
- Lightweight and direct: Objects interrogate the filesystem on demand; there is
  no caching layer or external index to maintain.
- Uniqueness without a DB: For one-more-level deep workspaces, the
  ``UniqueSubdirFactory`` ensures unique, sortable subdirectories based on a
  timestamp+counter encoding scoped per day.

When this shines
----------------
This approach is ideal for systems that create hundreds (but probably not
thousands) of temporary directories per day, for example:
- ETL or data processing jobs
- Report generation pipelines
- Media processing/transcoding tasks
- Long-running offline jobs coordinated by a worker process

It provides a human-first structure that supports:
- Developer debugging (quickly find the latest directory)
- Log research (locate directories for a specific date)
- Cleanup (purge old days to free disk space)
- Focusing on current activity (navigate by day and then by creation order)

Core concepts
-------------
- ``folder_pattern``: A forward-slash-delimited path pattern using strftime
  directives (e.g., ``"%Y/%m/%d"``, ``"Project X/%Y-%m/%d-%a"``). The pattern must
  include the three date components ``%Y``, ``%m``, and ``%d`` in some order. Time
  directives (e.g., ``%H``, ``%M``) are not allowed in folder names.
- ``DateFolders``: The main API for constructing folders for specific dates,
  iterating ranges of dates, inferring dates from existing paths, and enforcing a
  retention policy.
- ``UniqueSubdirFactory``: A helper that creates one-more-level subdirectories at
  the bottom of a date folder, with unique base names that sort in creation order.

Quick start
-----------
Create a per-day directory tree and make today’s folder if needed:

```python
from pathlib import Path
import datetime
from totodev_pub.date_folders import DateFolders

root = Path("/var/app/tmp")
df = DateFolders("%Y/%m/%d", root_dir=root)

# Get today's folder (and create it)
today_dir = df.folder(datetime.date.today(), create=True)
print(today_dir)  # e.g., /var/app/tmp/2024/01/15
```

Create a unique subdirectory for a task under today’s date:

```python
factory = df.subdir_factory("workspaces")
w1 = factory.create("batch")   # /var/app/tmp/2024/01/15/workspaces/batch_ABCD
w2 = factory.create("batch")   # /var/app/tmp/2024/01/15/workspaces/batch_ABCD_01
w3 = factory.create("")        # /var/app/tmp/2024/01/15/workspaces/ABCD
```

Traverse existing folders in a date range (exclusive end):

```python
start = datetime.date(2024, 1, 1)
end   = datetime.date(2024, 2, 1)  # exclusive
for day_path in df.existing(start, end):
    print(day_path)
```

Infer a date from an arbitrary path (any depth) that embeds the pattern:

```python
date = df.infer_date("/var/app/tmp/2024/01/15/workspaces/batch_ABCD/output.log")
assert date.isoformat() == "2024-01-15"
```

Retention (age-based purge)
---------------------------
If you supply ``retain_days`` when constructing ``DateFolders``, a once-per-day
purge is triggered automatically the first time you create a folder on that day
and not within a short blackout near midnight. You can also drive purging
directly:

```python
df = DateFolders("%Y/%m/%d", root_dir=root, retain_days=14)

# Dry-run: see what would be removed
to_delete = df.purge_old(dry_run=True)

# Actual purge of content older than the cutoff
deleted = df.purge_old(dry_run=False)
```

Reasons and example scenario
---------------------------
Suppose your service spawns long-running media conversions and writes inputs,
intermediate files, and logs to per-job temp directories. Using Python’s default
temp directory names makes it hard to:
- Find the most recent run
- Cross-reference logs to a specific day
- Safely clean older directories without collateral damage

With this module:
- You set a root like ``/srv/media/tmp`` and a pattern ``"%Y/%m/%d"``.
- Operations staff browses by day to immediately see current activity.
- Developers can quickly jump to today’s or yesterday’s workspace and correlate
  with monitoring and logs.
- A nightly retention policy (e.g., keep last 14 days) is effortless and
  predictable.
- When you need multiple job directories per day, ``UniqueSubdirFactory`` creates
  unique, chronologically sortable names without a database or lock server.

CLI
---
Two commands are provided when running this module directly:

- ``get_date_folder FOLDER_PATTERN DATE_STR ROOT_DIR``
  - Create (if needed) and print the folder path for the given date.
  - Example:
    ```bash
    python -m totodev_pub.date_folders get-date-folder "%Y/%m/%d" 2025-09-03 /var/app/tmp
    ```

- ``infer_date FOLDER_PATTERN PATH``
  - Print the ISO date inferred from ``PATH`` using the given pattern.
  - Example:
    ```bash
    python -m totodev_pub.date_folders infer-date "%Y/%m/%d" /var/app/tmp/2024/01/15/logs/app.log
    ```

Notes
-----
- Patterns must use forward slashes ``/`` regardless of OS; runtime will use the
  OS-specific separator when constructing real paths.
- Patterns must include ``%Y``, ``%m``, and ``%d``; time directives are not
  permitted in folder names.
- Choose patterns that alpha-sort chronologically (e.g., ``%Y-%m/%d``) when you
  plan to traverse existing folders by lexicographic order.
"""

import os
import re
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union
import datetime
from pydantic import BaseModel
import click


# Mapping of strftime directives to their regex patterns and capture requirements
# Used in path patterns
_STRFTIME_MATCHES = {
    '%Y': (r'(\d{4})', True),      # (pattern, should_capture)
    '%m': (r'(\d{1,2})', True),
    '%d': (r'(\d{1,2})', True),
    '%j': (r'\d+', False),          # Day of year
    '%U': (r'\d+', False),          # Week number (Sunday)
    '%W': (r'\d+', False),          # Week number (Monday)
    '%w': (r'\d+', False),          # Day of week
    '%a': (r'[A-Za-z]+', False),    # Abbreviated weekday
    '%A': (r'[A-Za-z]+', False),    # Full weekday
    '%b': (r'[A-Za-z]+', False),    # Abbreviated month
    '%B': (r'[A-Za-z]+', False),    # Full month
# Time entries are forbidden in folder names
#    '%H': (r'\d+', False),          # Hour
#    '%I': (r'\d+', False),          # Hour (12-hour)
#    '%M': (r'\d+', False),          # Minute
#    '%S': (r'\d+', False),          # Second
#    '%p': (r'[A-Za-z]+', False),    # AM/PM
}


class _DateDirPart(BaseModel):
    """Represents a single part of a day directory pattern with its regex and capture information."""
    
    regex_pattern: str
    """The regex pattern for this part (may contain capture groups)"""
    
    original_text: str
    """The original text from the pattern before regex conversion"""
    
    capture_directives: List[str] = []
    """List of strftime directives that were converted to capture groups in this part"""
    
    @property
    def has_captures(self) -> bool:
        """Whether this part contains any capture groups."""
        return len(self.capture_directives) > 0
    
    def globify(self, year: Optional[int] = None, month: Optional[int] = None, day: Optional[int] = None) -> str:
        """
        Convert the original text to a glob pattern by replacing date placeholders.
        
        If a value is provided, it merges the value in. If a value isn't provided,
        it puts the corresponding glob numeric match pattern. Other strftime directives
        are replaced with '*' wildcards.
        
        Args:
            year: Year value to use for %Y replacement, or None for glob pattern
            month: Month value to use for %m replacement, or None for glob pattern  
            day: Day value to use for %d replacement, or None for glob pattern
            
        Returns:
            String with date placeholders replaced by values or glob patterns,
            and other strftime directives replaced with '*' wildcards
            
        Examples:
            - globify(year=2024) on "%Y-%m-%d" -> "2024-[0-9][0-9]-[0-9][0-9]"
            - globify(month=12) on "%Y/%m/%d-%a" -> "[0-9][0-9][0-9][0-9]/12/[0-9][0-9]-*"
            - globify() on "%Y-%m-%d-%b" -> "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-*"
        """
        result = self.original_text
        
        # Replace %Y with year value or glob pattern
        if year is not None:
            result = result.replace('%Y', str(year))
        else:
            result = result.replace('%Y', '[0-9][0-9][0-9][0-9]')
        
        # Replace %m with month value or glob pattern
        if month is not None:
            result = result.replace('%m', f"{month:02d}")
        else:
            result = result.replace('%m', '[0-9][0-9]')
        
        # Replace %d with day value or glob pattern
        if day is not None:
            result = result.replace('%d', f"{day:02d}")
        else:
            result = result.replace('%d', '[0-9][0-9]')
        
        # Replace all other strftime directives with * wildcards
        # This covers %j, %U, %W, %w, %a, %A, %b, %B, etc.
        import re
        result = re.sub(r'%[A-Za-z]', '*', result)
        
        return result
    
    @staticmethod
    def is_probably_well_ordered(parts: List['_DateDirPart']) -> bool:
        """
        Detect whether the date components (%Y, %m, %d) occur in chronological order from left to right.
        
        This is a rough gauge of whether an alpha sort of directory paths is probably equivalent 
        to a chronological sort of the directory paths.
        
        Args:
            parts: List of _DateDirPart objects representing the folder pattern parts
            
        Returns:
            True if the date components appear in %Y, %m, %d order from left to right,
            False otherwise.
            
        Examples:
            - ["%Y", "%m", "%d"] -> True (well-ordered)
            - ["%Y", "%d", "%m"] -> False (not well-ordered)
            - ["%m", "%Y", "%d"] -> False (not well-ordered)
            - ["%Y", "%m", "%d", "extra"] -> True (well-ordered)
            - ["Project", "%Y-%m", "%d-%a"] -> True (well-ordered, directives in order)
        """
        # Find the positions of date components in the pattern
        date_positions = {}
        
        # Build the full pattern string to find actual directive positions
        full_pattern = "/".join(part.original_text for part in parts)
        
        # Find the first occurrence of each date directive in the full pattern
        for directive in ['%Y', '%m', '%d']:
            pos = full_pattern.find(directive)
            if pos != -1:  # Directive found
                date_positions[directive] = pos
        
        # Check if we have all three date components
        if len(date_positions) < 3:
            return False
        
        # Check if they appear in chronological order: %Y before %m before %d
        y_pos = date_positions.get('%Y')
        m_pos = date_positions.get('%m')
        d_pos = date_positions.get('%d')
        
        if y_pos is None or m_pos is None or d_pos is None:
            return False
        
        return y_pos < m_pos < d_pos

###### END class _DateDirPart ##############################

class _DateFolderPather:
    """
    Utility class is used to:
      - construct a folder path for a given date
      - infer the date from a folder path
      - validate that a folder path matches a given pattern

    """
    # Default directory separator for the OS
    _default_dir_sep = os.sep
    
    @classmethod
    def set_directory_separator(cls, separator: str) -> None:
        """
        Set the default directory separator for new instances.
        
        Args:
            separator: The directory separator to use (e.g., '/' or '\\')
        """
        cls._default_dir_sep = separator
    
    @classmethod
    def reset_directory_separator(cls) -> None:
        """Reset the default directory separator to the OS default."""
        cls._default_dir_sep = os.sep
    
    def is_probably_well_ordered(self) -> bool:
        """
        Check if the date components in this folder pattern are in chronological order.
        
        This method delegates to the static method _DateDirPart.is_probably_well_ordered()
        to determine whether the pattern will sort chronologically.
        
        Returns:
            True if the date components appear in %Y, %m, %d order from left to right,
            False otherwise.
            
        Examples:
            - "%Y/%m/%d" -> True (well-ordered)
            - "%Y/%d/%m" -> False (not well-ordered)
            - "Project/%Y/%m/%d" -> True (well-ordered with prefix)
        """
        return _DateDirPart.is_probably_well_ordered(self._dir_parts)
    @staticmethod
    def build_pattern_regex_parts(folder_pattern: str) -> List['_DateDirPart']:
        """
        Build _DateDirPart objects from a folder pattern string.
        
        Args:
            folder_pattern: String pattern with forward slashes and strftime directives
            
        Returns:
            List of _DateDirPart objects containing regex patterns and capture information
            
        Note:
            - Trailing forward slash is stripped before processing
            - %Y, %m, %d get capture groups
            - Other strftime directives get alphanumeric wildcards
        """
        parts = folder_pattern.split("/")
        regex_parts = []
        for part in parts:
            regex_part = part
            capture_directives = []
            
            # Find all strftime directives in this part
            strftime_matches = re.findall(r'%[A-Za-z]', part)
            for directive in strftime_matches:
                pattern, should_capture = _STRFTIME_MATCHES.get(directive, (r'[A-Za-z0-9]+', False))
                regex_part = regex_part.replace(directive, pattern)
                if should_capture:
                    capture_directives.append(directive)
            
            # Create _DateDirPart object
            day_dir_part = _DateDirPart(
                regex_pattern=regex_part,
                original_text=part,
                capture_directives=capture_directives
            )
            regex_parts.append(day_dir_part)
        
        return regex_parts

    def __init__(self, folder_pattern:str):
        # Set the folder pattern first, then validate it
        self.folder_pattern = folder_pattern.rstrip('/')
        
        # verify that the pattern contains %Y, %m, %d in some permutation or raise an error
        missing_components = [comp for comp in ["%Y", "%m", "%d"] if comp not in self.folder_pattern]
        if missing_components:
            raise ValueError(f"Folder pattern must contain %Y, %m, %d in some permutation. Missing: {', '.join(missing_components)}")
        
        # Raise an error if any time-centric directives appear in the string
        time_directives = ["%H", "%I", "%M", "%S", "%p"]
        found_directives = [comp for comp in time_directives if comp in self.folder_pattern]
        if found_directives:
            raise ValueError(f"Folder pattern cannot contain time-centric directives. Found: {', '.join(found_directives)}")
        
        # Raise an error if the first character is a separator
        if self.folder_pattern.startswith('/') or self.folder_pattern.startswith('\\'):
            raise ValueError("Folder pattern cannot start with a separator")
        
        # Raise an error if backslashes are found in the pattern
        # Folder patterns must use forward slashes regardless of OS for consistency
        if '\\' in self.folder_pattern:
            raise ValueError("Folder pattern must use forward slashes (/) as separators, not backslashes (\\)")

        
        # Build and compile the regex pattern
        self._dir_parts: List[_DateDirPart] = self.build_pattern_regex_parts(self.folder_pattern)
        # Use instance-specific directory separator
        self._dir_sep = self._default_dir_sep
        dir_sep = re.escape(self._dir_sep) # the directory separator for this instance
        regex_patterns = [part_obj.regex_pattern for part_obj in self._dir_parts]
        
        # Track capture group positions for date components
        # Note: group(1) is the outer wrapper (^|/), group(2) is the entire pattern, so date components start at group(3)
        self._date_capture_positions = {}
        capture_count = 2  # Start at 2 since group(1) is outer wrapper, group(2) is the entire pattern
        for part_obj in self._dir_parts:
            if part_obj.has_captures:
                for directive in part_obj.capture_directives:
                    capture_count += 1
                    self._date_capture_positions[directive] = capture_count
        
        # Wrap in capture group and allow matching within larger paths
        # (^|/)pattern($|/) - matches start of string or after separator, and end of string or before separator
        self._regexp = re.compile(f"(^|{dir_sep})(" + dir_sep.join(regex_patterns) + f")($|{dir_sep})")


    def construct_folder_path(self, year: int, month: int, day: int, offset_days: int = 0) -> Path:
        """
        Construct a folder path for a given year, month, and day.

        Directory separators are replaced with the current OS separator.

        Args:
            year: The year to construct the folder path for
            month: The month to construct the folder path for
            day: The day to construct the folder path for
            offset_days: The number of days to offset the date by (default is 0)
        """
        # Create a date object to use with strftime
        date_obj = datetime.date(year, month, day) + datetime.timedelta(days=offset_days)
        
        # Process each part of the pattern
        processed_parts = []
        for part_obj in self._dir_parts:
            # Replace strftime directives with actual values
            processed_part = date_obj.strftime(part_obj.original_text)
            processed_parts.append(processed_part)
        
        # Join with OS-specific separator and return as Path
        return Path(self._dir_sep.join(processed_parts))
    

    def globify(self, year: Optional[int] = None, month: Optional[int] = None, day: Optional[int] = None) -> List[Path]:
        """
        Convert the folder pattern to a list of glob patterns for incremental directory walking.
        
        Each Path in the returned list represents one level of the directory structure that can be
        used for step-by-step traversal. Non-wildcard path elements are merged with following
        wildcard elements to eliminate unnecessary walking steps.
        
        Args:
            year: Year value to use for %Y replacement, or None for glob pattern
            month: Month value to use for %m replacement, or None for glob pattern  
            day: Day value to use for %d replacement, or None for glob pattern
            
        Returns:
            List of Path objects representing incremental glob patterns for directory walking.
            Each Path can be used to find matching directories at that level of the structure.
            
        Examples:
            Pattern "xyz/%Y%a/%m-%d/abc":
            - globify() -> [Path("xyz/[0-9][0-9][0-9][0-9]*"), Path("[0-9][0-9]-[0-9][0-9]"), Path("abc")]
            - globify(year=2027, day=7) -> [Path("xyz/2027*"), Path("[0-9][0-9]-07"), Path("abc")]
            
        Note:
            - Non-wildcard path elements are merged with following wildcard elements
            - This allows for efficient incremental directory traversal
            - Each returned Path represents a meaningful choice point in the directory structure
        """
        # Convert each directory part using the _DateDirPart.globify method
        glob_parts = []
        for part_obj in self._dir_parts:
            glob_part = part_obj.globify(year, month, day)
            glob_parts.append(glob_part)
        
        # Merge non-wildcard parts with following parts after interpolation
        # This reduces the number of glob searches during directory descent
        merged_parts = []
        i = 0
        while i < len(glob_parts):
            current_part = glob_parts[i]
            
            # Check if current part has wildcards
            has_wildcards = any(char in current_part for char in ['*', '[', '?'])
            
            if has_wildcards:
                # Current part has wildcards, keep it separate
                merged_parts.append(Path(current_part))
                i += 1
            else:
                # Current part has no wildcards, merge with next part
                if i + 1 < len(glob_parts):
                    merged_part = current_part + self._dir_sep + glob_parts[i + 1]
                    merged_parts.append(Path(merged_part))
                    i += 2  # Skip both parts since we merged them
                else:
                    # Last part with no wildcards, add it as-is
                    merged_parts.append(Path(current_part))
                    i += 1
        
        return merged_parts
        
        return merged_parts

    def infer_date_from_folder_path(self, folder_path: str) -> Optional[datetime.date]:
        """
        Infer the date from a folder or file path located at the day position or lower.

        Examples pattern : path : date
        "%Y/%m/%d" : "2024/01/01" : 2024-01-01
        "/some/root/folder/2024/01/01" : 2024-01-01
        "/some/root/folder/2024/01/01/sub/subsub/file.txt" : 2024-01-01
        "/some/root/folder/2024/01/01/file.txt" : 2024-01-01

        In the above, replace forward slash with the current OS separator.

        Args:
            folder_path: The folder path to infer the date from

        Returns:
            The date inferred from the folder path, or None if the folder path does not match the pattern
        """
        match = self._regexp.search(folder_path)
        if match: # Extract date components using the tracked capture positions
            #entire_date_string = match.group(2) # not used at this time
            year = int(match.group(self._date_capture_positions['%Y']))
            month = int(match.group(self._date_capture_positions['%m']))
            day = int(match.group(self._date_capture_positions['%d']))
            try:
                return datetime.date(year, month, day)
            except ValueError:
                # Invalid date (e.g., day 32 for month 1)
                return None
        else:
            return None  # indicates folder match could not be made


############ END class _DateFolderPather ############


class UniqueSubdirFactory:
    """
    Factory for creating unique, chronologically-sortable subdirectories within date folders.
    
    This factory is linked to a DateFolders instance and delegates all date handling to it.
    Uniqueness is achieved through timestamp + counter encoding, scoped to individual dates.
    
    This class is particularly useful for:
    - Creating isolated workspaces for different subsystems or processes
    - Managing temporary or intermediate data directories that need to be unique
    - Organizing log files, cache directories, or temporary workspaces by date
    - Ensuring that multiple processes can create directories without naming conflicts
    
    The factory creates subdirectories with names that automatically sort chronologically
    when listed alphabetically, making it easy to browse directories in creation order.
    
    Attributes:
        date_folders: The parent DateFolders instance this factory is linked to
        subdir_fragment: Path fragment to append to date folders
        anchor_date: Reference date for subdirectory creation (None = current date)
    
    Example:
        Given a DateFolders instance with pattern "%Y-%m/%d" and root "/data/logs":
        
        # Create a factory for error logs
        error_factory = df.subdir_factory("errors")
        
        # Create some subdirectories
        dir1 = error_factory.create("connection_failed")     # -> /data/logs/2024-01-15/errors/connection_failed_A1B2
        dir2 = error_factory.create("timeout_error")         # -> /data/logs/2024-01-15/errors/timeout_error_A1B2
        dir3 = error_factory.create("")                      # -> /data/logs/2024-01-15/errors/A1B2
        
        # If multiple directories are created in the same tenth-of-a-second:
        dir4 = error_factory.create("connection_failed")     # -> /data/logs/2024-01-15/errors/connection_failed_A1B2_01
        dir5 = error_factory.create("connection_failed")     # -> /data/logs/2024-01-15/errors/connection_failed_A1B2_02
        
        # Directory listing will show them in creation order:
        # A1B2                           (created first)
        # connection_failed_A1B2         (created second)  
        # connection_failed_A1B2_01      (created third)
        # connection_failed_A1B2_02      (created fourth)
        # timeout_error_A1B2             (created last)
    """
    
    # 57-character alphabet for encoding (removing 'l','o', 'O', '0' for clarity)
    _ALPHANUMERIC_CHARS = "123456789ABCDEFGHIJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz"
    
    def __init__(self, 
                 date_folders: "DateFolders",
                 subdir_fragment: Union[str, Path],
                 anchor_date: Optional[datetime.date] = None):
        """
        Initialize the factory.
        
        Args:
            date_folders: Parent DateFolders instance
            subdir_fragment: Path fragment for subdirectories
            anchor_date: Reference date (None = current date)
        """
        self.date_folders = date_folders
        self.subdir_fragment = Path(subdir_fragment)
        self.anchor_date = anchor_date
    
    def _encode_number(self, number: int, min_length: int = 1) -> str:
        """
        Encode a number using the custom 58-character alphabet.
        
        Args:
            number: Number to encode
            min_length: Minimum length of encoded string
        
        Returns:
            str: Encoded string
        """
        if number == 0:
            return self._ALPHANUMERIC_CHARS[0] * min_length
        
        result = ""
        base = len(self._ALPHANUMERIC_CHARS)
        
        while number > 0:
            number, remainder = divmod(number, base)
            result = self._ALPHANUMERIC_CHARS[remainder] + result
        
        # Pad to minimum length
        while len(result) < min_length:
            result = self._ALPHANUMERIC_CHARS[0] + result
        
        return result
    
    def _get_timestamp_encoding(self) -> str:
        """
        Get the current timestamp encoded as 4 characters.
        
        Returns:
            str: 4-character encoded timestamp (tenths of a second since midnight)
        """
        now = datetime.datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tenths_since_midnight = int((now - midnight).total_seconds() * 10)
        return self._encode_number(tenths_since_midnight, min_length=4)
    
    def create(self, dir_basename: str = "") -> Path:
        """
        Create a unique subdirectory for the current anchor date.
        
        Args:
            dir_basename: Base name for the subdirectory (can be empty string)
        
        Returns:
            Path: Path to the created subdirectory
            
        Example:
            # Create with custom name
            dir1 = factory.create("batch")  # -> /var/logs/2024-01-15/errors/batch_A1B2
            
            # Create with empty name
            dir2 = factory.create("")  # -> /var/logs/2024-01-15/errors/A1B2
            
            # Create with timestamp-based name
            dir3 = factory.create()  # -> /var/logs/2024-01-15/errors/A1B2
            
            # If multiple directories are created in the same tenth-of-a-second:
            dir4 = factory.create("batch")  # -> /var/logs/2024-01-15/errors/batch_A1B2_01
            dir5 = factory.create("batch")  # -> /var/logs/2024-01-15/errors/batch_A1B2_02
        """
        # Get the date folder (create if it doesn't exist)
        target_date = self.anchor_date or datetime.date.today()
        date_folder = self.date_folders.folder(target_date, create=True)
        
        # Construct the base subdirectory path
        subdir_path = date_folder / self.subdir_fragment
        
        # Ensure the subdirectory exists
        subdir_path.mkdir(parents=True, exist_ok=True)
        
        # Generate timestamp encoding
        timestamp_encoding = self._get_timestamp_encoding()
        
        # Try to create without counter first
        if dir_basename:
            base_name = f"{dir_basename}_{timestamp_encoding}"
        else:
            base_name = timestamp_encoding
        
        target_path = subdir_path / base_name
        
        # If no collision, create and return
        if not target_path.exists():
            target_path.mkdir()
            return target_path
        
        # Handle collision by adding counter
        counter = 1
        while True:
            counter_encoding = self._encode_number(counter, min_length=2)
            if dir_basename:
                collision_name = f"{dir_basename}_{timestamp_encoding}_{counter_encoding}"
            else:
                collision_name = f"{timestamp_encoding}_{counter_encoding}"
            
            collision_path = subdir_path / collision_name
            
            if not collision_path.exists():
                collision_path.mkdir()
                return collision_path
            
            counter += 1


class DateFolders:
    """
    This class is used to manage a folder structure in which each date has a specific folder.
    The folder structure is governed by a pattern suitable for use with strftime. 
    It's main use is in systems storing data by date, centered around the current date.
    Note that this class uses and returns by preference: datetime.date objects, Path objects

    The class has facilities for:
    - Calculating a folder path for a given date (creating if necessary)
    - Sequentially iterating over EXPECTED folders within a date range (forward and reverse)
    - Sequentially interating over EXISTING folders within a date range (forward and reverse)
    - Age-based folder deletion (i.e. simple retention policy)
    - Interrogation of folder contents (e.g. is_empty(), categories_on_date())

    Example Patterns:
       "%Y/%m/%d"  # folders like ".../2024/01/01"  (YYYY/MM/DD)
       "%Y-%m-%d"  # folders like ".../2024-01-01"  (YYYY-MM-DD)
       "%Y-%m/%d-%a"  # folders like ".../2024/01/01-Mon"  (YYYY/MM/DD-Mon)
       "Project X/Yearly/%Y/%m-%b/%d-%a"  # Extra prefix to make it clear what project the folders are for
                               # folders like ".../Project X/Yearly/2024/01-Jan/01-Mon"

    Note that when choosing a pattern you may find it convenient to choose one that alpha-sorts correctly

    If allow_categories is True, then the folder structure will have a category folder within the date folder.
    This is useful for projects that have multiple categories of data. Which might produce folders like:
    - ".../2024/01/01-Mon/cat1" ".../2024/01/01-Mon/cat2"
    - ".../Project X/2024/01/01-Mon/cat1" ".../Project X/2024/01/01-Mon/cat2"

    Note: 
    - While patterns MUST use a forwardslash to denote a folder, the class will work with separator of the current OS.
    - It's a good idea to choose patterns that alpha-sort correctly (but not required).
    - DayFolders is tolerant of files and folders that are not part of the expected structure (e.g. if a folder is created by another process) but it may screw up if the extra folders look too much like the expected structure.  Use with care.
    - Patterns MUST contain at least %Y, %m, %d in some permutation.


    """

    # Add a class variable for a dict that maps folder_patterns to _DateDirPart (for caching)
    _folder_pattern_to_dir_parts: dict[str, _DateFolderPather] = {}

    def __init__(self, 
                 folder_pattern:str,
                 root_dir:Optional[Union[str, Path]]=None,
                 anchor_date:Optional[datetime.date]=None,
                 retain_days:Optional[int]=None
                ):
        """
        Initialize a DayFolders instance for managing date-based folder structures.
        
        Args:
            folder_pattern: strftime-compatible pattern for folder structure (e.g., "%Y/%m/%d-%a")
                            Note that the folder pattern will be stripped of leading and trailing forwardslashes
            root_dir: Base directory for the folder structure. Must exist, but date subfolders 
                     will be created as needed. If None, uses current working directory.
            anchor_date: Reference date for interpreting relative date ranges in some functions (e.g. +1 means one day after anchor date)
            retain_days: Number of days to retain folders (i.e. how long to keep them before deleting).
                         Zero means only the current day's folder will be retained.
        """
        self.folder_pattern = folder_pattern.strip('/')
        
        # Convert to Path and validate existence
        self.root_dir = Path(root_dir) if root_dir is not None else Path.cwd()
        
        # Check if the root directory exists
        if not self.root_dir.exists():
            # Check if the parent directory exists (everything except the last component)
            parent_dir = self.root_dir.parent
            if not parent_dir.exists():
                raise FileNotFoundError(f"Parent directory '{parent_dir}' does not exist on the local filesystem")
            
            # Auto-create the final directory component
            self.root_dir.mkdir(parents=False, exist_ok=True)
        
        self.anchor_date = anchor_date
        self.retain_days = retain_days
        if folder_pattern not in self._folder_pattern_to_dir_parts:
            self._folder_pattern_to_dir_parts[folder_pattern] = _DateFolderPather(folder_pattern)
        self._pather: _DateFolderPather = self._folder_pattern_to_dir_parts[folder_pattern]

    def subdir_factory(self, 
                                 subdir_fragment: Union[str, Path],
                                 anchor_date: Optional[datetime.date] = None) -> "UniqueSubdirFactory":
        """
        Creates a factory for generating unique subdirectories within date folders.
        
        This method creates a DatedUniqueSubdirectoryFactory instance that is linked to
        this DateFolders instance and can create unique, chronologically-sortable subdirectories.
        
        Args:
            subdir_fragment: Path fragment to append to date folders (e.g., "logs/errors", "data/processed")
            anchor_date: Reference date for subdirectory creation. None uses current date.
        
        Returns:
            DatedUniqueSubdirectoryFactory: A factory instance linked to this DateFolders instance.
        
        Example:
            # Create a DateFolders instance with pattern "%Y-%m/%d"
            df = DateFolders("%Y-%m/%d", root_dir="/var/logs")
            
            # Create a factory for error log subdirectories
            error_log_factory = df.subdir_factory("errors")
            
            # Create unique subdirectories for today
            error_dir1 = error_log_factory.create("connection_failed")
            error_dir2 = error_log_factory.create("timeout_error")
            
            # Directories will be created as:
            # /var/logs/2024-01-15/errors/connection_failed_A1B2
            # /var/logs/2024-01-15/errors/timeout_error_A1B2
            
            # Create a factory for temporary data processing
            temp_factory = df.subdir_factory("temp/processing")
            temp_dir = temp_factory.create("batch_001")
            # -> /var/logs/2024-01-15/temp/processing/batch_001_A1B2
        """
        return UniqueSubdirFactory(
            date_folders=self,
            subdir_fragment=subdir_fragment,
            anchor_date=anchor_date
        )

    def folder(self, dte: Union[int, datetime.date], anchor_date: Optional[datetime.date] = None, create: bool = False) -> Path:
        """
        Get the folder path for a specific date.
        
        Args:
            dte: Either a datetime.date object representing the target date, or an integer 
                 representing the offset in days from the anchor date (e.g., 0 for today, 
                 -1 for yesterday, +1 for tomorrow)
            anchor_date: Optional anchor date to use for offset calculations. If provided, 
                        overrides the instance anchor_date. If None, uses the instance 
                        anchor_date or current system date.
            create: If True, force the directory into existence before returning the path.
                   If False, return the path without creating the directory.
        
        Returns:
            Path object representing the folder path for the specified date
            
        Note:
            - If dte is an integer, it represents days offset from the anchor_date
            - If anchor_date parameter is provided, it takes precedence over instance anchor_date
            - If no anchor_date is available, the current system date is used as the anchor
            - The folder will only be created if create=True
        """
        # Handle integer offset from anchor date
        if isinstance(dte, int):
            # Determine which anchor date to use (parameter takes precedence)
            if anchor_date is not None:
                anchor = anchor_date
            elif self.anchor_date is not None:
                anchor = self.anchor_date
            else:
                anchor = datetime.date.today()
            target_date = anchor + datetime.timedelta(days=dte)
        else:
            # dte is already a datetime.date object
            target_date = dte
        
        # Build the folder path using the pather
        folder_path = self._pather.construct_folder_path(target_date.year, target_date.month, target_date.day)
        
        # Combine with root directory (always required now)
        full_path = self.root_dir / folder_path
        
        # Create the folder if requested
        if create:
            full_path.mkdir(parents=True, exist_ok=True)
            
            # Conditional retention purge trigger
            # - only when create=True
            # - never within MIDNIGHT_PURGE_BLACKOUT minutes of midnight
            # - at most once per day per root_dir
            MIDNIGHT_PURGE_BLACKOUT = 20
            now = datetime.datetime.now()
            minutes_since_midnight = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60.0
            if minutes_since_midnight > MIDNIGHT_PURGE_BLACKOUT:
                # Initialize last-run cache
                if not hasattr(self.__class__, "_last_purge_run_dates"):
                    self.__class__._last_purge_run_dates = {}
                root_key = str(self.root_dir.resolve())
                today = now.date()
                last_run = self.__class__._last_purge_run_dates.get(root_key)
                if last_run != today:
                    # Execute purge_old (no-op if retain_days unset)
                    try:
                        self.purge_old(dry_run=False)
                    finally:
                        # Record attempt regardless of outcome to limit once per day
                        self.__class__._last_purge_run_dates[root_key] = today
        
        return full_path
    
    def infer_date(self, folder_path: Union[str, Path]) -> datetime.date:
        """
        Infer the date from a folder or file path by matching it against the configured folder pattern.
        
        This method analyzes the given path and attempts to extract a date based on the 
        strftime pattern used to create the folder structure. It can handle paths that 
        contain the day folder anywhere within them, as long as the date components 
        match the expected pattern.
        
        Args:
            folder_path: Either a string path or Path object representing the folder or file path
                        to analyze for date extraction. Can be a full path, relative path, or 
                        just the folder name itself.
        
        Returns:
            datetime.date: The date extracted from the folder path
            
        Raises:
            ValueError: If the folder path cannot be matched to a specific date. This can happen when:
                       - The path doesn't contain the expected date pattern (%Y/%m/%d)
                       - The path structure doesn't match the configured folder pattern
                       - The extracted date components form an invalid date (e.g., month 13, day 32)
                       - The path is empty or contains no recognizable date information
        
        Examples:
            Given pattern "%Y/%m/%d":
            - "2024/01/15" → datetime.date(2024, 1, 15)
            - "/some/root/2024/01/15/file.txt" → datetime.date(2024, 1, 15)
            - "2024/01/15/subfolder" → datetime.date(2024, 1, 15)
            
            Given pattern "Project X/%Y-%m-%d":
            - "Project X/2024-01-15" → datetime.date(2024, 1, 15)
            - "/path/to/Project X/2024-01-15/data/file.txt" → datetime.date(2024, 1, 15)
        """
        # Convert Path to string if needed
        path_str = str(folder_path) if isinstance(folder_path, Path) else folder_path
        
        # Use the pather to infer the date
        inferred_date = self._pather.infer_date_from_folder_path(path_str)
        
        if inferred_date is None:
            # Provide a detailed error message explaining why the match failed
            pattern_info = f"pattern '{self.folder_pattern}'"
            path_info = f"path '{path_str}'"
            
            raise ValueError(
                f"Could not infer date from {path_info} using {pattern_info}. "
                f"The path must contain date components that match the expected format. "
                f"Ensure the path includes the date structure defined by the pattern "
                f"and that the date components form a valid date."
            )
        
        return inferred_date

    def folders(self, 
                         start_date: Union[int, datetime.date],
                         end_date: Union[int, datetime.date], 
                         existing_only: bool = True, 
                         reverse: bool = False,
                         anchor_date: Optional[datetime.date] = None) -> Iterator[Path]:
        """
        Generator that yields folder paths for dates in the specified range.
        
        Args:
            start_date: Either a datetime.date object or an integer representing days offset 
                       from the anchor date (e.g., 0 for today, -1 for yesterday)
            end_date: Either a datetime.date object or an integer representing days offset 
                     from the anchor date (e.g., 7 for a week from anchor). Note: end_date 
                     is EXCLUSIVE - the range includes start_date but not end_date.
            existing_only: If True, only yield folders that exist on the filesystem
            reverse: If True, yield folders in reverse chronological order
            anchor_date: Optional anchor date to use for offset calculations. If provided,
                        overrides the instance anchor_date. If None, uses the instance
                        anchor_date or current system date.
            
        Note:
            The date range follows Python's half-open interval convention: [start_date, end_date).
            For example, folders_in_range(0, 7) with anchor_date=today will yield folders
            for days 0, 1, 2, 3, 4, 5, and 6 (7 total), but not day 7.

        Note: 
            The code uses a sequential probe through the dates in the range.
            If the range is large and sparsely populated, this may not be the best approach.
            
        Note:
            When existing_only=True and the folder pattern is well-ordered, this method
            automatically delegates to existing_in_range() for better performance on
            sparsely populated date ranges.
        """
        # Optimization: If we're only looking for existing folders and the pattern is well-ordered,
        # delegate to existing_in_range() for better performance
        if existing_only and self._pather.is_probably_well_ordered():
            yield from self.existing(start_date, end_date, reverse=reverse, anchor_date=anchor_date)
            return
        
        # Convert integer offsets to actual dates using the same logic as date_folder
        def _resolve_anchor(offset: int) -> datetime.date:
            anchor = anchor_date or self.anchor_date or datetime.date.today()
            return anchor + datetime.timedelta(days=offset)
        
        start_date, end_date = [ _resolve_anchor(d) if isinstance(d, int) else d
                                 for d in [start_date, end_date]
                               ]
        
        if reverse:
            step_size = -1
            d0 = end_date - datetime.timedelta(days=1)  # Start from day before end_date
            d1 = start_date - datetime.timedelta(days=1)  # End at day before start_date
        else:
            step_size = 1
            d0 = start_date  # Start from start_date
            d1 = end_date  # End at end_date (exclusive)
        
        current_date = d0
        while current_date != d1:
            folder_path = self.folder(current_date)
            if not existing_only or folder_path.exists():
                yield folder_path
            current_date += datetime.timedelta(days=step_size)

    def existing(self, 
                         start_date: Union[int, datetime.date],
                         end_date: Union[int, datetime.date], 
                         reverse: bool = False,
                         anchor_date: Optional[datetime.date] = None) -> Iterator[Path]:
        """
        Generator that yields existing folder paths for dates in the specified range.
        
        This method uses the globify() functionality to traverse existing folders on the
        filesystem, rather than sequentially probing through expected dates. It requires
        the underlying folder pattern to be "well ordered" for proper chronological traversal.
        
        Args:
            start_date: Either a datetime.date object or an integer representing days offset 
                       from the anchor date (e.g., 0 for today, -1 for yesterday)
            end_date: Either a datetime.date object or an integer representing days offset 
                     from the anchor date (e.g., 7 for a week from anchor). Note: end_date 
                     is EXCLUSIVE - the range includes start_date but not end_date.
            reverse: If True, yield folders in reverse chronological order
            anchor_date: Optional anchor date to use for offset calculations. If provided,
                        overrides the instance anchor_date. If None, uses the instance
                        anchor_date or current system date.
            
        Returns:
            Iterator yielding existing folder paths in chronological order
            
        Raises:
            ValueError: If the underlying folder pattern is not "well ordered" (date components
                       must appear in %Y, %m, %d order from left to right for proper sorting)
            
        Note:
            The date range follows Python's half-open interval convention: [start_date, end_date).
            For example, existing_in_range(0, 7) with anchor_date=today will yield folders
            for days 0, 1, 2, 3, 4, 5, and 6 (7 total), but not day 7.
            
            This method is more efficient than folders_in_range() for sparsely populated
            date ranges as it only traverses existing directories rather than probing
            through all expected dates.
        """
        # Check if the folder pattern is well ordered for proper chronological traversal
        if not self._pather.is_probably_well_ordered():
            raise ValueError(
                f"Folder pattern '{self.folder_pattern}' is not well ordered. "
                f"Date components must appear in %Y, %m, %d order from left to right "
                f"for proper chronological sorting when using existing_in_range()."
            )
        
        # Convert integer offsets to actual dates using the same logic as date_folder
        def _resolve_anchor(offset: int) -> datetime.date:
            anchor = anchor_date or self.anchor_date or datetime.date.today()
            return anchor + datetime.timedelta(days=offset)
        
        start_date, end_date = [ _resolve_anchor(d) if isinstance(d, int) else d
                                 for d in [start_date, end_date]
                               ]
        
        # Optimization: If start and end dates are in the same year, pass that year to globify
        # to narrow the search scope and improve performance
        year_param = None
        month_param = None
        
        if start_date.year == end_date.year:
            year_param = start_date.year
            # Additional optimization: If month is also the same, pass that too
            if start_date.month == end_date.month:
                month_param = start_date.month
        
        # Get the glob patterns for incremental directory walking
        glob_patterns = self._pather.globify(year=year_param, month=month_param)
        
        # Start with the root directory
        current_base = self.root_dir
        
        # Use a recursive helper to traverse the directory structure
        def _traverse_directories(base_path: Path, pattern_index: int, target_date: datetime.date) -> Iterator[Path]:
            if pattern_index >= len(glob_patterns):
                # We've reached the end of the pattern, check if this path matches our date criteria
                try:
                    inferred_date = self._pather.infer_date_from_folder_path(str(base_path))
                    if inferred_date is not None:
                        # Check if the date is within our range
                        if reverse:
                            if start_date <= inferred_date < end_date:
                                yield base_path
                        else:
                            if start_date <= inferred_date < end_date:
                                yield base_path
                except ValueError:
                    # Path doesn't match date pattern, skip it
                    pass
                return
            
            current_pattern = glob_patterns[pattern_index]
            
            # Find all directories matching the current pattern
            try:
                matching_dirs = list(base_path.glob(str(current_pattern)))
                # Sort directories for consistent traversal order
                matching_dirs.sort()
                
                if reverse:
                    matching_dirs.reverse()
                
                for matching_dir in matching_dirs:
                    if matching_dir.is_dir():
                        # Recursively traverse deeper into the directory structure
                        yield from _traverse_directories(matching_dir, pattern_index + 1, target_date)
            except (OSError, PermissionError):
                # Skip directories we can't access
                pass
        
        # Traverse all directories starting from the root
        yield from _traverse_directories(current_base, 0, start_date)



    def purge(self, 
                       start_date: Union[int, datetime.date],
                       end_date: Union[int, datetime.date],
                       anchor_date: Optional[datetime.date] = None,
                       yesterday_tolerance_minutes: int = 20) -> None:
        """
        Delete all date folders within the specified range and clean up empty parent directories.
        
        This method uses existing_in_range() to find existing date folders, deletes them,
        and then traverses upward through the directory tree to remove any empty directories.
        The root directory (self.root_dir) is never deleted.
        
        Args:
            start_date: Either a datetime.date object or an integer representing days offset 
                       from the anchor date (e.g., 0 for today, -1 for yesterday)
            end_date: Either a datetime.date object or an integer representing days offset 
                     from the anchor date (e.g., 7 for a week from anchor). Note: end_date 
                     is EXCLUSIVE - the range includes start_date but not end_date.
            anchor_date: Optional anchor date to use for offset calculations. If provided,
                        overrides the instance anchor_date. If None, uses the instance
                        anchor_date or current system date.
            yesterday_tolerance_minutes: Minutes after midnight during which yesterday's
                        folder will be protected from deletion. This helps avoid deleting
                        data created shortly before midnight that may still be in use.
        
        Note:
            The date range follows Python's half-open interval convention: [start_date, end_date).
            For example, purge_in_range(0, 7) with anchor_date=today will delete folders
            for days 0, 1, 2, 3, 4, 5, and 6 (7 total), but not day 7.
            
        Raises:
            ValueError: If the underlying folder pattern is not "well ordered" (required for existing_in_range)
            OSError: If there are permission issues or the filesystem is read-only
            
        Example:
            Given a structure like:
            /root/2024/01/15-Mon/data/file1.txt
            /root/2024/01/16-Tue/data/file2.txt
            /root/2024/02/01-Thu/data/file3.txt
            
            purge_in_range(2024-01-15, 2024-01-17) will:
            1. Delete /root/2024/01/15-Mon/ and contents
            2. Delete /root/2024/01/16-Tue/ and contents  
            3. Delete /root/2024/01/ (now empty)
            4. Keep /root/2024/ (still has /02/ subdirectory)
            5. Keep /root/ (still has /2024/ subdirectory)
        """
        import shutil
        
        # Convert integer offsets to actual dates using the same logic as other methods
        def _resolve_anchor(offset: int) -> datetime.date:
            anchor = anchor_date or self.anchor_date or datetime.date.today()
            return anchor + datetime.timedelta(days=offset)
        
        start_date, end_date = [ _resolve_anchor(d) if isinstance(d, int) else d
                                 for d in [start_date, end_date]
                               ]
        
        # Determine if we should protect yesterday's folder shortly after midnight
        now = datetime.datetime.now()
        minutes_since_midnight = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60.0
        protect_yesterday = minutes_since_midnight <= max(0, yesterday_tolerance_minutes)
        reference = anchor_date or self.anchor_date or datetime.date.today()
        yesterday = reference - datetime.timedelta(days=1)
        
        # Get all existing date folders in the range
        date_folders = list(self.existing(start_date, end_date, anchor_date=anchor_date))
        
        # Optionally filter out yesterday's folder if within tolerance window
        if protect_yesterday:
            filtered = []
            for folder_path in date_folders:
                try:
                    inferred = self._pather.infer_date_from_folder_path(str(folder_path))
                except Exception:
                    inferred = None
                if inferred == yesterday:
                    # Skip deleting yesterday's folder quietly
                    continue
                filtered.append(folder_path)
            date_folders = filtered
        
        # Track which parent directories might become empty
        affected_parents = set()
        
        # Delete each date folder and track its parent directories
        for folder_path in date_folders:
            try:
                # Add all parent directories to the affected set for potential cleanup
                current_path = folder_path
                while current_path != self.root_dir and current_path.parent != current_path:
                    affected_parents.add(current_path.parent)
                    current_path = current_path.parent
                
                # Delete the date folder and all its contents
                if folder_path.exists():
                    shutil.rmtree(folder_path)
                    
            except (OSError, PermissionError) as e:
                # Log the error but continue with other folders
                import warnings
                warnings.warn(f"Failed to delete folder {folder_path}: {e}")
        
        # Clean up empty parent directories, starting from the deepest level
        # Sort by path depth (deepest first) to ensure proper cleanup order
        sorted_parents = sorted(affected_parents, key=lambda p: len(p.parts), reverse=True)
        
        for parent_path in sorted_parents:
            try:
                # Only delete if the directory exists and is completely empty
                if (parent_path.exists() and 
                    parent_path.is_dir() and 
                    parent_path != self.root_dir and
                    not any(parent_path.iterdir())):
                    
                    parent_path.rmdir()
                    
            except (OSError, PermissionError) as e:
                # Log the error but continue with other directories
                import warnings
                warnings.warn(f"Failed to delete empty directory {parent_path}: {e}")

    def purge_old(self, dry_run: bool = False) -> List[Path]:
        """
        Enforce the retention policy by deleting folders older than retain_days.
        
        This method uses the retain_days value that was passed in when the object was constructed.
        It finds all folders older than the retention period and either deletes them or returns
        a list of what would be deleted (if dry_run is True).
        
        Args:
            dry_run: If True, build the list of folders to be deleted but don't actually delete them.
                     If False, delete the folders and return the list of deleted folders.
        
        Returns:
            List[Path]: List of folder paths that were deleted (or would be deleted in dry_run mode)
        
        Note:
            - If retain_days is None or less than 0, this method is a no-op and returns an empty list
            - The method uses existing_in_range() to find existing folders and purge_in_range() for deletion
            - Empty parent directories are automatically cleaned up after deletion
        """
        # If no retention policy is set, return empty list
        if self.retain_days is None or self.retain_days < 0:
            return []
        
        # Calculate the cutoff date
        reference_date = self.anchor_date or datetime.date.today()
        cutoff_date = reference_date - datetime.timedelta(days=self.retain_days)
        
        # Get all folders that would be deleted (older than cutoff_date)
        # Use a very early date (1900-01-01) as the start date to get all folders
        earliest_date = datetime.date(1900, 1, 1)
        folders_to_delete = list(self.existing(
            start_date=earliest_date,  # Start from a very early date
            end_date=cutoff_date,  # Up to but not including cutoff_date
            anchor_date=reference_date
        ))
        
        # If dry_run, just return the list
        if dry_run:
            return folders_to_delete
        
        # Otherwise, actually delete the folders
        if folders_to_delete:
            # Use purge_in_range to delete folders and clean up empty directories
            self.purge(
                start_date=earliest_date,  # Start from a very early date
                end_date=cutoff_date,  # Up to but not including cutoff_date
                anchor_date=reference_date
            )
        
        return folders_to_delete


@click.command()
@click.argument('folder_pattern')
@click.argument('date_str')
@click.argument('root_dir')
def get_date_folder(folder_pattern: str, date_str: str, root_dir: str) -> str:
    """
    Get the path to a date folder for the specified pattern and date.
    
    FOLDER_PATTERN: strftime-compatible pattern for folder structure (e.g., "%Y/%m/%d", "%Y-%m-%d")
    DATE_STR: Date in YYYY-MM-DD format (e.g., "2025-09-03")
    ROOT_DIR: Root directory for the folder structure
    
    The directory will be created automatically if it does not exist.
    """
    try:
        # Parse the date string
        target_date = datetime.date.fromisoformat(date_str)
        
        # Create DateFolders instance
        df = DateFolders(folder_pattern, root_dir=root_dir)
        
        # Get the date folder path (always create if it doesn't exist)
        folder_path = df.folder(target_date, create=True)
        
        # Output the result
        click.echo(str(folder_path))
        return str(folder_path)
        
    except ValueError as e:
        click.echo(f"Error: Invalid date format. Expected YYYY-MM-DD, got '{date_str}'", err=True)
        raise click.BadParameter(f"Invalid date format: {e}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@click.command()
@click.argument('folder_pattern')
@click.argument('path')
def infer_date(folder_pattern: str, path: str) -> str:
    """
    Infer the date from a folder or file path using the specified folder pattern.
    
    FOLDER_PATTERN: strftime-compatible pattern for folder structure (e.g., "%Y/%m/%d", "%Y-%m-%d")
    PATH: Path to analyze for date extraction
    """
    try:
        # Create DateFolders instance (root_dir doesn't matter for inference)
        df = DateFolders(folder_pattern, root_dir="/tmp")
        
        # Infer the date from the path
        inferred_date = df.infer_date(path)
        
        # Output the result in ISO format
        result = inferred_date.isoformat()
        click.echo(result)
        return result
        
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


if __name__ == '__main__':
    # Create a command group for multiple commands
    @click.group()
    def cli():
        """DateFolders command-line interface."""
        pass
    
    # Add commands to the group
    cli.add_command(get_date_folder)
    cli.add_command(infer_date)
    
    # Run the CLI
    cli()
