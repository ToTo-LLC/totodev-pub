# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Code Quality Sweep Tool - TotoDev Project Standards

This module provides a flexible and extensible tool for scanning Python codebases to identify
common code quality issues and anti-patterns. It uses a plugin-based architecture where each
type of check is implemented as a separate class.

Current checks include:
- DEBUG/FIXME/DIAG comments
- Naked breakpoint() calls
- Python filenames with capital letters
- Hardcoded absolute filepaths
- Non-CamelCase class names

Adding New Checks:
1. Create a new class that inherits from BaseCheck
2. Implement the required 'description' property
3. Override one or both of these methods:
   - check_line(): For line-by-line checks
   - check_file(): For whole-file checks
4. Optionally override:
   - applies_to_file(): To limit which files the check runs on

Example of adding a new check:

    class TodoCheck(BaseCheck):
        @property
        def description(self) -> str:
            return "TODO comments without ticket numbers"
            
        def check_line(self, line: str, line_no: int, filepath: str) -> Optional[List[str]]:
            if "TODO" in line and not re.search(r'TODO.*#\\d+', line):
                return [f"{filepath}:{line_no}: TODO comment without ticket number"]
            return None

The check will be automatically discovered and included in the scanning process.

Usage:
    python -m totodev_pub.minor.sweep [directory]
    
For more details on each check, see the individual check class documentation.
"""

import argparse
import os
import subprocess
import glob
import re
import pathspec
import inspect
from abc import ABC, abstractmethod
from typing import Optional, Generator, List, Set, Protocol, runtime_checkable
import logging
#from .flexargs import FlexArgs

_g_pattern = re.compile(r'#\s*(DEBUG|FIXME|DIAG)')
_g_acceptable_diag = re.compile(r'^\s*#.*#\s*DIAG') # commented out diag is okay
_g_filepath_pattern = re.compile(r'(?<!r)(?<!R)["\'][\\/].*([\\/].*){2,}["\'](?:\s*)$')
_g_class_pattern = re.compile(r'^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)')

# Constants for exceptions
ALLOWED_CAPITAL_FILENAMES: Set[str] = {
    'README.md',
    'Dockerfile',
    'Makefile',
    'URLconf.py',
    '__init__.py',
    'setup.py',
    'MANIFEST.in',
    'LICENSE',
}

ALLOWED_SYSTEM_PATHS: Set[str] = {
    '/usr/local/bin',
    '/etc/hosts',
    '/dev/null',
    '/tmp',
    '/var/log',
}

# Pattern to detect if a filepath is being assigned to a constant
_g_constant_assignment = re.compile(r'^\s*[A-Z][A-Z0-9_]*\s*=\s*["\'][\\/]')

@runtime_checkable
class CodeCheck(Protocol):
    """Protocol defining the interface for all code checks."""
    @property
    def description(self) -> str:
        """Description of what this check looks for."""
        ...

    def check_line(self, line: str, line_no: int, filepath: str) -> Optional[List[str]]:
        """Check a single line of code."""
        ...
        
    def check_file(self, filepath: str) -> Optional[List[str]]:
        """Check an entire file without reading its contents."""
        ...

    def applies_to_file(self, filepath: str) -> bool:
        """
        Additional file-specific checks beyond extension matching.
        Only called if the file extension matches one in applied_to_extensions().
        """
        return True
        
    def applied_to_extensions(self) -> List[str]:
        """
        List of file extensions this check applies to (e.g., ['.py', '.yaml']).
        This is checked before applies_to_file() as a first-pass filter.
        """
        return ['.py']

class BaseCheck(ABC):
    """Base class for all code checks providing default implementations."""
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what this check looks for."""
        pass

    def check_line(self, line: str, line_no: int, filepath: str) -> Optional[List[str]]:
        """Default implementation returns None - override for line-based checks."""
        return None
        
    def check_file(self, filepath: str) -> Optional[List[str]]:
        """Default implementation returns None - override for file-based checks."""
        return None

    def applies_to_file(self, filepath: str) -> bool:
        """Default implementation returns True - override for specific file checks."""
        return True
        
    def applied_to_extensions(self) -> List[str]:
        """Default to Python files only."""
        return ['.py']
        
    @staticmethod
    def parse_gitignore(gitignore_path: str) -> pathspec.PathSpec:
        """Parse a .gitignore file into a PathSpec object.
        
        Args:
            gitignore_path: Path to the .gitignore file
            
        Returns:
            A PathSpec object containing the gitignore patterns
        """
        with open(gitignore_path, 'r') as file:
            gitignore = file.read()
        return pathspec.PathSpec.from_lines('gitwildmatch', gitignore.splitlines())
        
    @staticmethod
    def check_filename_case(filepath: str) -> Optional[List[str]]:
        """
        Check if a Python file contains capital letters in its name, with exceptions for
        common conventions and standard files.
        
        Args:
            filepath: The path to the file being checked
            
        Returns:
            Optional[List[str]]: A list containing the error message if the filename has capital letters, None otherwise
        """
        filename = os.path.basename(filepath)
        if filename in ALLOWED_CAPITAL_FILENAMES:
            return None
            
        if filename.endswith('.py') and any(c.isupper() for c in filename):
            return [f"{filepath}: Filename contains capital letters which is not recommended for Python files"]
        return None

    @staticmethod
    def check_hardcoded_filepath(line: str, line_no: int, filepath: str) -> Optional[List[str]]:
        """
        Check if a line contains an uncommented hardcoded absolute filepath.
        Allows for system paths, constant assignments, and files with 'config' in their name.
        
        Args:
            line: The line to check
            line_no: The line number being checked
            filepath: The path to the file being checked
            
        Returns:
            Optional[List[str]]: A list containing the error message if a hardcoded filepath is found, None otherwise
        """
        # Skip config files
        if 'config' in os.path.basename(filepath).lower():
            return None
            
        # Skip comments and docstrings
        stripped_line = line.lstrip()
        if stripped_line.startswith('#') or stripped_line.startswith('"""') or stripped_line.startswith("'''"):
            return None
            
        # Skip if it's a constant assignment
        if _g_constant_assignment.search(line):
            return None
            
        # Skip lines that don't end with a quote (after stripping whitespace)
        stripped_end = line.rstrip()
        if not stripped_end or stripped_end[-1] not in '"\'':
            return None
            
        if _g_filepath_pattern.search(line):
            for allowed_path in ALLOWED_SYSTEM_PATHS:
                if allowed_path in line:
                    return None
            return [f"{filepath}:{line_no}: Contains hardcoded absolute filepath: {line.strip()}"]
        return None

    @staticmethod
    def check_class_name_case(line: str, line_no: int, filepath: str) -> Optional[List[str]]:
        """
        Check if a class declaration follows proper CamelCase naming convention.
        Validates both first letter capitalization and full CamelCase format.
        Allows private classes (starting with underscore) to use any naming convention.
        
        Args:
            line: The line to check
            line_no: The line number being checked
            filepath: The path to the file being checked
            
        Returns:
            Optional[List[str]]: A list containing the error message if improper class naming is found, None otherwise
        """
        if line.lstrip().startswith('#'):
            return None
            
        if match := _g_class_pattern.search(line):
            class_name = match.group(1)
            
            # Skip private classes
            if class_name.startswith('_'):
                return None
                
            if not class_name[0].isupper():
                return [f"{filepath}:{line_no}: Class name '{class_name}' should use CamelCase (start with capital letter)"]
            
            if '_' in class_name:
                if not (os.path.basename(filepath).startswith('test_') and class_name.startswith('Test_')):
                    return [f"{filepath}:{line_no}: Class name '{class_name}' should use CamelCase (avoid underscores)"]
        return None

class CommentCheck(BaseCheck):
    @property
    def description(self) -> str:
        return "DEBUG, FIXME, and DIAG comments (except properly commented-out DIAG)"
        
    def applied_to_extensions(self) -> List[str]:
        """Comments will only be checked in specific configuration and script files."""
        return [
            '.py',     # Python files
            '.yaml',   # YAML files
            '.yml',    # Alternative YAML extension
            '.toml',   # TOML files
            '.env',    # Environment files
            '.sh',     # Shell scripts
            '.bash'    # Bash scripts
        ]
        
    def check_line(self, line: str, line_no: int, filepath: str) -> Optional[List[str]]:
        if (m := _g_pattern.search(line)):
            if m.group(1) != 'DIAG' or not _g_acceptable_diag.search(line):
                return [f"{filepath}:{line_no}: {line.strip()}"]
        return None

class BreakpointCheck(BaseCheck):
    @property
    def description(self) -> str:
        return "Naked breakpoint() method calls in Python files"
        
    def check_line(self, line: str, line_no: int, filepath: str) -> Optional[List[str]]:
        # Skip docstrings (both single and triple quoted)
        stripped = line.lstrip()
        if stripped.startswith('"""') or stripped.startswith("'''") or stripped.startswith('#'):
            return None
            
        if 'breakpoint()' in line and not re.search(r"""\s*(#|.*'|.*")""", line[:line.index('breakpoint()')]):
            return [f"{filepath}:{line_no}: {line.strip()}"]
        return None

class FilenameCheck(BaseCheck):
    @property
    def description(self) -> str:
        return "Python filenames containing capital letters"
        
    def applies_to_file(self, filepath: str) -> bool:
        return filepath.endswith('.py')
        
    def check_file(self, filepath: str) -> Optional[List[str]]:
        filename = os.path.basename(filepath)
        if filename in ALLOWED_CAPITAL_FILENAMES:
            return None
            
        if any(c.isupper() for c in filename):
            return [f"{filepath}: Filename contains capital letters which is not recommended for Python files"]
        return None

class HardcodedPathCheck(BaseCheck):
    @property
    def description(self) -> str:
        return "Hardcoded absolute filepaths in non-config files"
        
    def check_line(self, line: str, line_no: int, filepath: str) -> Optional[List[str]]:
        if 'config' in os.path.basename(filepath).lower():
            return None
            
        stripped_line = line.lstrip()
        if stripped_line.startswith('#') or stripped_line.startswith('"""') or stripped_line.startswith("'''"):
            return None
            
        if _g_constant_assignment.search(line):
            return None
            
        # Skip lines that don't end with a quote (after stripping whitespace)
        stripped_end = line.rstrip()
        if not stripped_end or stripped_end[-1] not in '"\'':
            return None
            
        if _g_filepath_pattern.search(line):
            # Extract the path from the line
            match = re.search(r'["\']([/\\].+?)["\']', line)
            if match:
                path = match.group(1)
                # Check if the path is an allowed system path
                for allowed_path in ALLOWED_SYSTEM_PATHS:
                    if path.startswith(allowed_path):
                        return None
            return [f"{filepath}:{line_no}: Contains hardcoded absolute filepath: {line.strip()}"]
        return None

class ClassNameCheck(BaseCheck):
    @property
    def description(self) -> str:
        return "Non-CamelCase class names"
        
    def applies_to_file(self, filepath: str) -> bool:
        return filepath.endswith('.py')
        
    def check_line(self, line: str, line_no: int, filepath: str) -> Optional[List[str]]:
        if line.lstrip().startswith('#'):
            return None
            
        if match := _g_class_pattern.search(line):
            class_name = match.group(1)
            
            # Skip private classes
            if class_name.startswith('_'):
                return None
                
            if not class_name[0].isupper():
                return [f"{filepath}:{line_no}: Class name '{class_name}' should use CamelCase (start with capital letter)"]
            
            if '_' in class_name:
                if not (os.path.basename(filepath).startswith('test_') and class_name.startswith('Test_')):
                    return [f"{filepath}:{line_no}: Class name '{class_name}' should use CamelCase (avoid underscores)"]
        return None

def get_all_checks() -> List[CodeCheck]:
    """Discover all check classes in this module."""
    checks = []
    for name, obj in globals().items():
        if (inspect.isclass(obj) and 
            issubclass(obj, BaseCheck) and 
            obj != BaseCheck and 
            not inspect.isabstract(obj)):
            checks.append(obj())
    return checks

def find_files(directory: str, gitignore_spec: pathspec.PathSpec) -> list[str]:
    """Find all text files in the directory that should be checked.
    
    Args:
        directory: The directory to search in
        gitignore_spec: The gitignore patterns to respect
        
    Returns:
        A list of file paths to check
    """
    # Common text file extensions
    patterns = [
        '**/*.py',    # Python files
        '**/*.json',  # JSON files
        '**/*.yml',   # YAML files
        '**/*.yaml',  # Alternative YAML extension
        '**/*.cfg',   # Config files
        '**/*.ini',   # INI files
        '**/*.txt',   # Text files
        '**/*.md',    # Markdown files
        '**/*.rst',   # ReStructuredText files
        '**/*.toml',  # TOML files
        '**/*.xml',   # XML files
        '**/*.html',  # HTML files
        '**/*.css',   # CSS files
        '**/*.js',    # JavaScript files
        '**/*.ts',    # TypeScript files
        '**/*.sh',    # Shell scripts
        '**/*.bash',  # Bash scripts
        '**/*.env',   # Environment files
    ]
    files = [file for pattern in patterns for file in glob.glob(os.path.join(directory, pattern), recursive=True)]
    return [file for file in files if not gitignore_spec.match_file(file)]

def run_pep8_check(directory: str):
    #TODO: Exclusions below just don't seem to work.... keep pulling in the .c9 subdirectory
    # attempt to exlude stuff in the .gitignore
    result = subprocess.run("grep -v '^#' .gitignore | tr '\n' ','", shell=True, check=True, capture_output=True, text=True)
    subprocess.run(['flake8', f'"--exclude={result.stdout}"',directory],shell=True)

def scan_for_issues(directory: str) -> Generator[str, None, None]:
    """
    Recursively scans the given directory and all its subdirectories for code issues,
    yielding issue messages as they are found.
    
    The scan looks for various issues defined by the check classes in this module.
    New checks can be added by creating new classes that inherit from BaseCheck.
    
    Args:
        directory: The root directory path to scan recursively
        
    Yields:
        str: Issue messages in the format "filepath:line_no: issue_description"
        
    Raises:
        OSError: If there are problems accessing files or directories
    """
    try:
        if os.path.exists(os.path.join(directory, '.gitignore')):
            gitignore_spec = BaseCheck.parse_gitignore(os.path.join(directory, '.gitignore'))
        else:
            gitignore_spec = pathspec.PathSpec.from_lines('gitwildmatch', [])

        files_to_check = find_files(directory, gitignore_spec)
        checks = get_all_checks()
        
        for filepath in files_to_check:
            if filepath == __file__ or re.match(r'^test_.*\.py$', os.path.basename(filepath)):
                continue  # we don't scan test files.
                
            try:
                file_ext = os.path.splitext(filepath)[1].lower()
                
                applicable_checks = [
                    check for check in checks 
                    if file_ext in check.applied_to_extensions() and check.applies_to_file(filepath)
                ]
                
                # Run file-level checks first
                for check in applicable_checks:
                    if file_issues := check.check_file(filepath):
                        yield from file_issues
                
                # Then run line-level checks
                with open(filepath, 'r') as file:
                    in_triple_quotes = False
                    triple_quote_type = None  # Will be ''' or """
                    
                    for line_no, line in enumerate(file, 1):
                        if not in_triple_quotes:
                            # Look for start of triple quotes, but only if not inside a regular string
                            # This is a simple heuristic - it might miss some edge cases
                            if ('"""' in line and line.count('"') >= 3) or ("'''" in line and line.count("'") >= 3):
                                # Determine which type of quote we found first
                                quote_pos_double = line.find('"""')
                                quote_pos_single = line.find("'''")
                                
                                if quote_pos_double != -1 and (quote_pos_single == -1 or quote_pos_double < quote_pos_single):
                                    triple_quote_type = '"""'
                                else:
                                    triple_quote_type = "'''"
                                    
                                # If we don't find a matching end quote on this line, we're entering triple quote mode
                                if line.count(triple_quote_type) == 1:
                                    in_triple_quotes = True
                                    continue
                                
                            # Run checks on this line
                            for check in applicable_checks:
                                try:
                                    if line_issues := check.check_line(line, line_no, filepath):
                                        yield from line_issues
                                except Exception as e:
                                    yield f"Error in {check.__class__.__name__} processing {filepath}:{line_no}: {str(e)}"
                        else:
                            # We're inside a triple-quoted string
                            if triple_quote_type and triple_quote_type in line:
                                # Found the end of the triple-quoted string
                                end_quote_pos = line.find(triple_quote_type)
                                in_triple_quotes = False
                                
                                # If there's content after the closing quotes, check that
                                if end_quote_pos != -1:
                                    remaining_line = line[end_quote_pos + 3:]
                                    if remaining_line.strip():  # If there's non-whitespace content
                                        for check in applicable_checks:
                                            try:
                                                if line_issues := check.check_line(remaining_line, line_no, filepath):
                                                    yield from line_issues
                                            except Exception as e:
                                                yield f"Error in {check.__class__.__name__} processing {filepath}:{line_no}: {str(e)}"
                                triple_quote_type = None
                                    
            except Exception as e:
                yield f"Error reading file {filepath}: {str(e)}"
                
    except Exception as e:
        yield f"Error scanning directory {directory}: {str(e)}"

def main():
    parser = argparse.ArgumentParser(description='Sweep for specific comments and optional PEP 8 compliance.')
    parser.add_argument('directory', nargs='?', default=os.getcwd(), help='Directory to sweep')
    parser.add_argument('-p', '--pep8', action='store_true', help='Run PEP 8 compliance check')
    args = parser.parse_args()

    logger = logging.getLogger(__name__)

    # Print issues as they are found
    for issue in scan_for_issues(args.directory):
        logger.info(issue)

    if args.pep8:
        run_pep8_check(args.directory)

if __name__ == "__main__":
    main()
