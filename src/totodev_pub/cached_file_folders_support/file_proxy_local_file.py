# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Local file proxy implementation for CachedFileFolders.

This module provides classes for working with local files in the CachedFileFolders system:

## Major Classes

### LocalFileProxy
A proxy for files that exist locally on the filesystem. This class implements the FileProxyBase
interface and provides methods for materialization (which is immediate for local files),
deployment to target directories, and file comparison operations.

### LocalFileProxyFactory
A factory class for discovering and creating LocalFileProxy objects using glob patterns.
This class provides lazy scanning of the local filesystem and supports both recursive
and non-recursive pattern matching with symlink control.

## Usage Examples

### Basic LocalFileProxy Usage
```python
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxy

# Create a proxy for a local file
proxy = LocalFileProxy("/path/to/document.pdf")

# Materialize (immediate for local files)
import asyncio
result = asyncio.run(proxy.materialize(0.0))  # Always returns True

# Deploy to a target directory
proxy.deploy("/target/directory")  # Copies file to target

# Compare with another file
is_same = proxy.looks_same("/path/to/other/document.pdf")
```

### LocalFileProxyFactory Usage
```python
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxyFactory

# Create factory
factory = LocalFileProxyFactory()

# Scan for PDF files recursively
for proxy in factory.scan_files("**/*.pdf"):
    print(f"Found: {proxy.ref_path()}")
    # Process each file...

# Scan with symlink control
for proxy in factory.scan_files("documents/**/*.txt", follow_symlinks=True):
    print(f"Found: {proxy.ref_path()}")

# Batch processing for memory efficiency
for batch in factory.scan_files_batched("**/*.py", batch_size=50):
    for proxy in batch:
        print(f"Processing: {proxy.ref_path()}")
```

### Integration with CachedFileFolders
```python
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.file_proxy_local_file import LocalFileProxyFactory

# Create cache and factory
cache = CachedFileFolders("local_cache/", "/tmp/cache")
factory = LocalFileProxyFactory()

# Scan and cache local files
for proxy in factory.scan_files("documents/**/*.pdf"):
    change_notice = await cache.upsert_file(proxy, ["local", "documents"])
    if change_notice:
        print(f"Cached: {change_notice.file_path}")
```

## Pattern Examples
- `"*.txt"` - All .txt files in current directory
- `"**/*.pdf"` - All .pdf files recursively
- `"src/**/*.py"` - All .py files in src directory and subdirectories
- `"/absolute/path/*.docx"` - All .docx files in absolute path
"""

from typing import Optional, Dict, Any, Generator
from pathlib import Path
import shutil
import os

from .file_proxy_base import FileProxyBase


class LocalFileProxy(FileProxyBase):
    """
    Proxy for files that exist locally on the filesystem.
    """
    
    def __init__(self, local_path: str, ref_path: Optional[str] = None, delete_after_deploy: bool = False):
        """
        Initialize a local file proxy.
        
        Args:
            local_path: Path to the local file
            ref_path: Reference path (defaults to local_path if not provided)
            delete_after_deploy: Whether to delete the local file after deployment
        """
        self._local_path = local_path
        self._ref_path = ref_path if ref_path is not None else local_path
        self._delete_after_deploy = delete_after_deploy
        self._was_deployed = False

    def ref_path(self) -> str:
        return self._ref_path

    def deploy(self, target_dir: str) -> None:
        if self._was_deployed:
            raise RuntimeError("File has already been deployed")
        if target_dir == "/dev/null":
            if self._delete_after_deploy and os.path.exists(self._local_path):
                os.remove(self._local_path)
            self._was_deployed = True
            return
        if not os.path.isdir(target_dir):
            raise FileNotFoundError(f"Target directory does not exist: {target_dir}")
        filename = os.path.basename(self._ref_path)
        target_path = os.path.join(target_dir, filename)
        shutil.copy2(self._local_path, target_path)
        self._was_deployed = True
        if self._delete_after_deploy and os.path.exists(self._local_path):
            os.remove(self._local_path)

    def looks_same(self, other_fpath: str) -> Optional[bool]:
        try:
            local_stat = os.stat(self._local_path)
            other_stat = os.stat(other_fpath)
            return (local_stat.st_size == other_stat.st_size and local_stat.st_mtime == other_stat.st_mtime)
        except (OSError, IOError):
            return None

    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        # LocalFileProxy doesn't need materialization - file already exists locally
        return True

    def get_context_info(self) -> Dict[str, Any]:
        return {
            "proxy_type": "LocalFileProxy",
            "local_path": self._local_path,
            "ref_path": self._ref_path,
            "delete_after_deploy": self._delete_after_deploy,
            "was_deployed": self._was_deployed
        }



class LocalFileProxyFactory:
    """
    A factory class for creating LocalFileProxy objects using glob patterns.
    
    This class provides lazy scanning of the local filesystem using glob patterns
    to discover and create LocalFileProxy objects for matching files.
    
    ## Quick Start
    
    ```python
    factory = LocalFileProxyFactory()
    
    # Scan all PDF files recursively
    for proxy in factory.scan_files("**/*.pdf"):
        print(f"Found: {proxy.ref_path()}")
        # Proxy is ready for materialize() and deploy()
    ```
    
    ## Pattern Examples
    
    ```python
    # All PDF files recursively
    pdf_files = factory.scan_files("**/*.pdf")
    
    # Python files in src directory only
    src_python = factory.scan_files("src/**/*.py")
    
    # All files in current directory (non-recursive)
    current_files = factory.scan_files("*")
    
    # Specific directory with specific extension
    docs = factory.scan_files("documents/**/*.txt")
    ```
    
    ## Symlink Handling
    
    ```python
    # Follow symbolic links (default: False)
    files_with_symlinks = factory.scan_files("**/*.pdf", follow_symlinks=True)
    
    # Skip symbolic links (default behavior)
    files_no_symlinks = factory.scan_files("**/*.pdf", follow_symlinks=False)
    ```
    
    ## Integration with CachedFileFolders
    
    ```python
    from totodev_pub.cached_file_folders import CachedFileFolders
    
    # Create cache
    cache = CachedFileFolders("local_cache/", "/tmp/cache")
    
    # Scan and cache local files
    for proxy in factory.scan_files("documents/**/*.pdf"):
        change_notice = await cache.upsert_file(proxy, ["local", "documents"])
        if change_notice:
            print(f"Cached: {change_notice.file_path}")
    ```
    """
    
    def scan_files(self, pattern: str, follow_symlinks: bool = False) -> Generator[LocalFileProxy, None, None]:
        """
        Generator that scans the local filesystem using a glob pattern and yields LocalFileProxy objects.
        
        Args:
            pattern: Required glob pattern for file matching (e.g., "**/*.pdf", "src/**/*.py")
            follow_symlinks: Whether to follow symbolic links (default: False)
            
        Yields:
            LocalFileProxy: Configured proxy objects for matching files
            
        Raises:
            ValueError: If pattern is empty or None
            OSError: If there are filesystem access issues
        """
        if not pattern or not pattern.strip():
            raise ValueError("Pattern must be non-empty")
        
        pattern = pattern.strip()
        
        try:
            # Use pathlib.Path.glob for simpler and more robust pattern matching
            from pathlib import Path
            
            # Handle absolute paths by changing to that directory
            if os.path.isabs(pattern):
                # For absolute paths, we need to extract the base directory
                # Handle patterns like "/path/to/dir/*.txt" and "/path/to/dir/**/*.txt"
                pattern_path = Path(pattern)
                
                # Find the longest directory prefix that actually exists
                search_dir = None
                search_pattern = None
                
                # Try to find the base directory by walking up the path
                for parent in pattern_path.parents:
                    if parent.exists() and parent.is_dir():
                        search_dir = parent
                        # Get the relative pattern from this directory
                        search_pattern = str(pattern_path.relative_to(parent))
                        break
                
                if search_dir is None:
                    raise OSError(f"Cannot find valid directory for pattern: {pattern}")
                
                # Change to the directory and search
                original_cwd = os.getcwd()
                try:
                    os.chdir(str(search_dir))
                    matches = Path(".").glob(search_pattern)
                    # Convert relative matches to absolute paths
                    matches = [search_dir / match for match in matches]
                finally:
                    os.chdir(original_cwd)
            else:
                # Relative pattern - search from current directory
                matches = Path(".").glob(pattern)
            
            for match in matches:
                # Skip directories
                if match.is_dir():
                    continue
                
                # Skip symlinks if follow_symlinks is False
                if not follow_symlinks and match.is_symlink():
                    continue
                
                # Create proxy with absolute path
                proxy = LocalFileProxy(str(match.absolute()))
                yield proxy
                
        except Exception as e:
            raise OSError(f"Error scanning files with pattern '{pattern}': {e}")
    
    def scan_files_batched(
        self, 
        pattern: str, 
        batch_size: int = 100, 
        follow_symlinks: bool = False
    ) -> Generator[list[LocalFileProxy], None, None]:
        """
        Generator that yields batches of LocalFileProxy objects.
        
        Args:
            pattern: Required glob pattern for file matching
            batch_size: Number of files to include in each batch (default: 100)
            follow_symlinks: Whether to follow symbolic links (default: False)
            
        Yields:
            List[LocalFileProxy]: Batches of proxy objects
        """
        batch = []
        for proxy in self.scan_files(pattern, follow_symlinks):
            batch.append(proxy)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        
        if batch:
            yield batch
    
