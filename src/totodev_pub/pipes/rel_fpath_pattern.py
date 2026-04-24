# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Module for handling relative filepath patterns with wildcards.

This module simplifies common file management tasks by allowing you to define file patterns
once and reuse them throughout your application. Instead of scattering hardcoded paths and
manual string manipulation throughout your code, you can centralize pattern definitions and
let RelativeFilepathPattern handle the details.

Why Use RelativeFilepathPattern?
================================

1. **Centralize File Patterns**: Define your file structure once in configuration, then reuse
   patterns throughout your code without repeating paths.

2. **Safe Wildcard Substitution**: Generate filenames from templates (e.g., "report_*.pdf" 
   becomes "report_2024.pdf") without manual string manipulation and validation.

3. **Automatic Subdirectory Creation**: When generating output files, subdirectories are
   created automatically so you don't need mkdir() calls scattered everywhere.

4. **Pattern Validation**: Catches common mistakes early (absolute paths, path traversal with '..', 
   wildcards in directory names) with clear error messages.

5. **Flexible Root Folders**: Use the same pattern with different root folders (e.g., development
   vs. production paths) without redefining patterns.

Example Use Cases
=================

**Use Case 1: Application with Standard File Locations**

Instead of hardcoding paths throughout your application:

    # Without RelativeFilepathPattern (scattered, error-prone)
    input_dir = Path(root) / "inputs"
    input_dir.mkdir(exist_ok=True)
    csv_files = list(input_dir.glob("*.csv"))
    
    output_dir = Path(root) / "outputs"
    output_dir.mkdir(exist_ok=True)
    report_path = output_dir / "summary.json"

You can define patterns once and reuse them:

    # With RelativeFilepathPattern (centralized, reusable)
    PATTERNS = {
        'csv_inputs': RelativeFilepathPattern("inputs/*.csv", root_folder=root),
        'report': RelativeFilepathPattern("outputs/summary.json", root_folder=root)
    }
    
    # Find all matching files
    csv_files = PATTERNS['csv_inputs'].matched_files()
    
    # Generate output path (creates subdirectories automatically)
    report_path = PATTERNS['report'].calc_path()
    PATTERNS['report'].affirm_subdir()  # Ensures outputs/ exists

**Use Case 2: Dynamic Filename Generation from Templates**

When you need to generate filenames with dynamic components:

    # Without RelativeFilepathPattern (manual string manipulation)
    year = "2024"
    month = "10"
    filename = f"logs/report_{year}_{month}.pdf"
    log_dir = Path(root) / "logs"
    log_dir.mkdir(exist_ok=True)
    full_path = root / filename

You can use wildcard substitution:

    # With RelativeFilepathPattern (clean, validated)
    pattern = RelativeFilepathPattern("logs/report_*_*.pdf", root_folder=root)
    full_path = pattern.calc_path(merge=["2024", "10"])  # Creates logs/ dir too

**Use Case 3: Multi-Environment File Handling**

When the same patterns need to work across different root directories:

    # Define patterns once without hardcoding roots
    INPUT_PATTERN = RelativeFilepathPattern("data/raw/*.csv")
    OUTPUT_PATTERN = RelativeFilepathPattern("data/processed/result.json")
    
    # Use with dev environment
    dev_files = INPUT_PATTERN.matched_files(root_folder="/path/to/dev")
    dev_output = OUTPUT_PATTERN.calc_path(root_folder="/path/to/dev")
    
    # Same patterns work with production environment
    prod_files = INPUT_PATTERN.matched_files(root_folder="/path/to/prod")
    prod_output = OUTPUT_PATTERN.calc_path(root_folder="/path/to/prod")

Supported Pattern Examples
===========================
    - "inputs/*.pdf" - All PDF files in the inputs folder
    - "outputs/result.yaml" - A specific YAML file in outputs folder  
    - "process.log" - A log file in the current folder
    - "logs/logfile[0-9][0-9].log" - Numbered log files in logs folder
    - "reports/summary_*_*.csv" - Files with two wildcard components
"""

# rel_file_pattern.py
import fnmatch
from collections.abc import Generator
from pathlib import Path
from typing import Optional, Union


class RelativeFilepathPattern:
    """Represents a pattern of file naming within a single folder.

    For example some common sorts of patterns might be:
        "inputs/*.pdf" would indicate that all pdf files in the inputs folder are of this type.
        "outputs/result.yaml" would indicate that the report.yaml file in the outputs folder is of this type.
        "process.log" would indicate that the process.log file in the current folder is of this type.
        "logs/logfile[0-9][0-9].log" would indicate that the logfile0.log, logfile1.log, etc. files in the current folder are of this type.

    The class provides utility methods to find, verify, and generate files matching these patterns (given a "root" folder).
    As well as facilities for creating subdirectory paths to these files.
    """

    def __init__(self, pattern: str, nickname: Optional[str] = None, root_folder: Optional[Union[str, Path]] = None) -> None:
        """Initialize a RelativeFilePattern.

        Args:
            pattern: The glob pattern for the file type that may include a relative path
            nickname: The nickname of the file type
            root_folder: Optional root folder for file operations
        """
        self._validate_pattern(pattern)
        self._pattern = pattern
        self._nickname = nickname
        self._rel_path = Path(pattern)
        self.root_folder = Path(root_folder) if root_folder is not None else None

    def __eq__(self, other: object) -> bool:
        """Compare equality based on pattern only.

        nickname and root_folder are not considered for equality as they are external identifiers
        and operational details respectively.
        """
        if not isinstance(other, RelativeFilepathPattern):
            return NotImplemented
        return self._pattern == other._pattern

    def __hash__(self) -> int:
        """Generate hash based on pattern only.

        nickname and root_folder are not considered for hashing as they are external identifiers
        and operational details respectively.
        """
        return hash(self._pattern)

    def __repr__(self) -> str:
        """Return a string representation of the RelativeFilepathPattern.
        
        If a nickname is defined, it will be included in the representation.
        """
        if self._nickname:
            return f"RelativeFilepathPattern(nickname='{self._nickname}', pattern='{self._pattern}')"
        return f"RelativeFilepathPattern(pattern='{self._pattern}')"

    def _get_root_path(self, root_folder: Optional[Union[str, Path]] = None) -> Path:
        """Get the root path, using provided root_folder or instance's root_folder.
        
        Args:
            root_folder: Optional root folder override. If not provided, uses instance's root_folder.
            
        Returns:
            A Path object representing the root folder
            
        Raises:
            ValueError: If no root_folder is provided either to the method or constructor
        """
        if root_folder is None and self.root_folder is None:
            raise ValueError("root_folder must be provided either to the method or constructor")
        return Path(root_folder) if root_folder is not None else self.root_folder

    @property
    def pattern(self) -> str:
        """The glob pattern for the file type that may include a relative path"""
        return self._pattern

    @property
    def has_wildcards(self) -> bool:
        """Returns True if the pattern contains wildcards in the filename."""
        return self.wildcard_count() > 0

    @property
    def nickname(self) -> str:
        """Return the nickname of the file type, if one was provided."""
        return self._nickname
    
    def wildcard_count(self) -> int:
        """The number of '*' characters in the pattern's filename.
        
        Note: This is a method rather than a property to indicate it performs a calculation,
        though the calculation is simple. Use the has_wildcards property for a boolean check.
        """
        return self._rel_path.name.count('*')

    def calc_path(self, merge: Optional[list[str]] = None, root_folder: Optional[Union[str, Path]] = None, dir_only: bool = False) -> Path:
        """
        Calculates a path from the pattern, optionally merging additional path components.
        
        Args:
            merge: Optional components to substitute for '*' wildcards in the pattern's filename
            root_folder: Optional root folder override. If not provided, uses instance's root_folder.
            dir_only: If True, return the path to the directory only, not the file.
        Returns:
            A Path object representing the full path
        
        Raises:
            ValueError: If the number of '*' characters in the filename doesn't match the number of merge components
                      (only checked when dir_only=False)
            ValueError: If no root_folder is provided either to the method or constructor
        """
        if merge is None:
            merge = []
        
        root_path = self._get_root_path(root_folder)
        
        # If we only want the directory, no need to check merge components
        if dir_only:
            return root_path / self._rel_path.parent
            
        # Since wildcards can only be in the filename, we only need to check the name part
        num_stars = self._rel_path.name.count('*')
        if num_stars != len(merge):
            raise ValueError(f"The number of '*' in the filename ({num_stars}) does not match the number of merge components ({len(merge)})")
        
        # Build the path by joining root, subdirs, and the processed filename
        filename = self._rel_path.name
        for component in merge:
            filename = filename.replace('*', str(component), 1)
        return root_path / self._rel_path.parent / filename
    

    @staticmethod
    def _validate_pattern(pattern: str) -> None:
        path = Path(pattern)
        if path.is_absolute():
            raise ValueError(f"If the pattern contains directory path, it must be a relative path, but you provided an absolute path: {pattern}")
        if '..' in str(path.parts):
            raise ValueError(f"To prevent escape from the root, the pattern cannot contain '..', but you provided: {pattern}")
        # Check that wildcards and character classes are only used in the filename (last segment)
        for dir_part in path.parts[:-1]:  # Check all parts except the last
            if '*' in dir_part or '[' in dir_part:
                raise ValueError(f"To ensure only one directory is matched, the directory portions of the pattern may not contain wildcards or character classes, but you provided: {pattern}")

    def matched_files(self, root_folder: Optional[Union[str, Path]] = None) -> list[Path]:
        """
        Returns all files in the folder that match the pattern.

        Args:
            root_folder: Optional root folder override. If not provided, uses instance's root_folder.
        """
        root_path = self._get_root_path(root_folder)
        search_dir = root_path / self._rel_path.parent
        base_pattern = self._rel_path.name
        
        try:
            return [f for f in search_dir.iterdir() if fnmatch.fnmatch(f.name, base_pattern)]
        except FileNotFoundError:
            return []
        
    def is_match(self, file_path: Union[str, Path], root_folder: Optional[Union[str, Path]] = None) -> bool:
        """
        Returns True if the file path matches the pattern, being aware of the possible relative path.
        
        Note: The file_path is matched against the full pattern path (root_folder / pattern).
        The file_path can be absolute or relative, but must match the constructed full path string.
        
        Args:
            file_path: The path to check for a match (absolute or relative)
            root_folder: Optional root folder override. If not provided, uses instance's root_folder.
        """
        root_path = self._get_root_path(root_folder)
        search_dir = root_path / self._rel_path.parent
        base_pattern = self._rel_path.name
        return fnmatch.fnmatch(str(file_path), str(search_dir / base_pattern))

    def has_subdir(self) -> bool:
        """
        Returns True if the pattern has a subdirectory.
        """
        return str(self._rel_path.parent) != "."

    @staticmethod
    def affirm_subdirs(root_folder: Union[str, Path], patterns: list[Union[str, 'RelativeFilepathPattern']]) -> list[Path]:
        """
        Ensures that the subdirectories for all the given patterns exist.

        Args:
            root_folder: The root folder to create subdirectories in
            patterns: List of patterns, can be either strings or RelativeFilepathPattern instances

        Returns:
            List of subdirectories that were created or verified in same order as the patterns
        
        Raises:
            ValueError: If root_folder is not provided
        """
        if root_folder is None:
            raise ValueError("root_folder must be provided")
            
        root_path = Path(root_folder)
        
        subdirs = []
        for pattern in patterns:
            # Convert string patterns to RelativeFilepathPattern instances
            if isinstance(pattern, str):
                pattern = RelativeFilepathPattern(pattern)
                
            # Skip if no subdirectory is needed
            if not pattern.has_subdir():
                continue
                
            # Create the subdirectory
            search_dir = root_path / pattern._rel_path.parent
            search_dir.mkdir(parents=True, exist_ok=True)
            subdirs.append(search_dir)
        return subdirs
    
    def affirm_subdir(self, root_folder: Optional[Union[str, Path]] = None) -> Path:
        """
        Ensures that the subdirectory for the file type exists.
        
        Note: "affirm" is used to indicate this method ensures the directory exists,
        creating it if necessary. An alias create_subdir() is also available.
        
        Args:
            root_folder: Optional root folder override. If not provided, uses instance's root_folder.
            
        Returns:
            The subdirectory that was created or verified

        Raises:
            ValueError: If no root_folder is provided either to the method or constructor
        """
        root_path = self._get_root_path(root_folder)
        if not self.has_subdir():
            return root_path
            
        return self.affirm_subdirs(root_path, [self])[0]

    def create_subdir(self, root_folder: Optional[Union[str, Path]] = None) -> Path:
        """Delegates to affirm_subdir()"""
        return self.affirm_subdir(root_folder)

    @classmethod
    def from_dict(cls, named_patterns: dict[str, str], root_folder: Optional[Union[str, Path]] = None) -> Generator["RelativeFilepathPattern", None, None]:
        """
        Creates RelativeFilePattern objects from a dictionary of named patterns.
        
        Note: This method returns a generator for lazy iteration. Convert to list if needed.
        
        Args:
            named_patterns: Dictionary mapping nicknames to pattern strings
            root_folder: Optional root folder to set on all created instances
            
        Yields:
            RelativeFilepathPattern objects for each entry in the dictionary
        """
        for name, pattern in named_patterns.items():
            yield cls(pattern, name, root_folder)
