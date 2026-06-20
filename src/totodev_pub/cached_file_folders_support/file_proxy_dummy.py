# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Dummy file proxy implementation for testing purposes.

Contains:
- FileProxyDummyMockFailureError
- FileProxyDummy
"""

from typing import Optional, Dict, Any, Sequence
from pathlib import Path
import shutil
import os
import tempfile
import asyncio
import random

from .file_proxy_base import FileProxyBase, OriginMetadata


class FileProxyDummyMockFailureError(RuntimeError):
    """
    Custom exception raised by FileProxyDummy when simulating failures for testing.
    
    This exception is raised when forced_to_fail_counter > 0 to simulate
    network failures, authentication issues, or other remote file access problems
    during testing scenarios.
    """
    pass


class FileProxyDummy(FileProxyBase):
    """
    A dummy file proxy implementation for testing purposes.
    
    This class simulates remote file behavior with configurable delays, failures,
    and file content. It creates files with version numbers padded to 1KB and
    supports various testing scenarios including failure simulation and orphan
    file creation.
    """

    def __init__(
        self,
        ref_path: str,
        grouping_key: Optional[Sequence[str]] = None,
        version_num: int = 0,
        materialize_secs: float = 2.5,
        allow_pre_materialize_info: bool = False,
        forced_to_fail_counter: int = 0,
        orphan_tempfile: bool = False,
        init_mtime: Optional[float] = None
    ):
        """
        Initialize a dummy file proxy for testing.
        
        Args:
            ref_path: The reference path for the file
            grouping_key: Optional grouping key for test organization (readable convenience)
            version_num: Version number to write into the file content
            materialize_secs: Exact time for simulated materialization delay
            allow_pre_materialize_info: Whether to pre-materialize for looks_same() comparisons
            forced_to_fail_counter: Number of times to fail before succeeding (decremented on each failure)
            orphan_tempfile: If True, copy file (leaving orphan); if False, move file
            init_mtime: Initial modification time to set on the file when materialized
        """
        self._ref_path = ref_path
        self.grouping_key = grouping_key
        self.version_num = version_num
        self.materialize_secs = materialize_secs
        self.allow_pre_materialize_info = allow_pre_materialize_info
        self.forced_to_fail_counter = forced_to_fail_counter
        self.orphan_tempfile = orphan_tempfile
        self.init_mtime = init_mtime
        
        # Internal state
        self._local_file_path: Optional[str] = None
        self._was_deployed = False
        self._materialization_started = False
        self._materialization_completed = False
        self._file_mtime: Optional[float] = init_mtime
    
    def __del__(self):
        """Clean up temporary files when the object is garbage collected."""
        try:
            self.cleanup()
        except Exception:
            # Ignore errors during cleanup in __del__
            pass
        
    def ref_path(self) -> str:
        """Get the reference path for the file."""
        return self._ref_path
    
    def touch(self, mtime: float) -> None:
        """
        Set the modification time for the file.
        
        Args:
            mtime: Modification time as Unix timestamp
        """
        self._file_mtime = mtime
        if self._local_file_path and os.path.exists(self._local_file_path):
            os.utime(self._local_file_path, (mtime, mtime))
    
    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        """
        Create the dummy file with simulated delay and optional failure.
        
        Args:
            blocking_secs: Maximum time to block waiting for materialization
            temp_dir: Directory for temporary files (required for this implementation)
            
        Returns:
            True if the file has been successfully created and is ready for deployment.
            False if the materialization is still in progress or failed.
            
        Raises:
            ValueError: If temp_dir is None or blank.
            FileProxyDummyMockFailureError: If forced_to_fail_counter > 0.
        """
        if temp_dir is None or str(temp_dir) == "" or str(temp_dir) == ".":
            raise ValueError("temp_dir must be provided and non-blank for FileProxyDummy")
            
        if self._materialization_completed:
            return True
            
        if not self._materialization_started:
            self._materialization_started = True
            try:
                await self._create_dummy_file(temp_dir)
                self._materialization_completed = True
                return True
            except FileProxyDummyMockFailureError:
                # Reset the started flag so we can retry
                self._materialization_started = False
                # Re-raise our custom exception as-is
                raise
            except Exception as e:
                # Reset the started flag so we can retry
                self._materialization_started = False
                raise RuntimeError(f"Failed to create dummy file: {e}")
        
        # If materialization was started but not completed, wait for it
        if blocking_secs > 0:
            await asyncio.sleep(min(0.1, blocking_secs))
            return self._materialization_completed
        
        return False
    
    def deploy(self, target_dir: str) -> None:
        """
        Move or copy the dummy file to the target directory.
        
        Args:
            target_dir: Directory where the file should be deployed
            
        Raises:
            RuntimeError: If the file hasn't been materialized or has already been deployed.
        """
        if self._was_deployed:
            raise RuntimeError("File has already been deployed")
            
        if not self._materialization_completed or self._local_file_path is None:
            raise RuntimeError("File must be materialized before deployment")
            
        if target_dir == "/dev/null":
            # Just clean up the temporary file
            if os.path.exists(self._local_file_path):
                os.remove(self._local_file_path)
            self._was_deployed = True
            return
            
        # Fail if target directory does not exist
        if not os.path.isdir(target_dir):
            raise RuntimeError(f"Target directory does not exist: {target_dir}")
        
        # Get the filename from the ref_path, preserving extension
        filename = os.path.basename(self._ref_path)
        target_path = os.path.join(target_dir, filename)
        
        try:
            if self.orphan_tempfile:
                # Copy the file (leaving orphan)
                shutil.copy2(self._local_file_path, target_path)
            else:
                # Move the file
                shutil.move(self._local_file_path, target_path)
            
            # Set the modification time if we have that information
            if self._file_mtime is not None:
                os.utime(target_path, (self._file_mtime, self._file_mtime))
                
            self._was_deployed = True
            
        except Exception as e:
            raise RuntimeError(f"Failed to deploy file to {target_dir}: {e}")
    
    async def _create_dummy_file(self, temp_dir: Path) -> None:
        """
        Create the dummy file with version number content and simulated delay.
        
        Args:
            temp_dir: Directory where temporary files should be created
        """
        # Simulate exact delay using materialize_secs
        delay = self.materialize_secs
        await asyncio.sleep(delay)
        
        # Check for forced failure
        if self.forced_to_fail_counter > 0:
            self.forced_to_fail_counter -= 1
            raise FileProxyDummyMockFailureError(
                f"Simulated failure for testing (failures remaining: {self.forced_to_fail_counter})"
            )
        
        # Get filename with proper extension from ref_path
        filename = os.path.basename(self._ref_path)
        
        # Create temporary file in the provided temp_dir
        temp_fd, temp_path = tempfile.mkstemp(suffix=os.path.splitext(filename)[1], dir=str(temp_dir))
        
        try:
            # Write version number padded to 1KB
            content = str(self.version_num)
            padded_content = content.ljust(1024, ' ')
            
            with os.fdopen(temp_fd, 'w') as f:
                f.write(padded_content)
            
            # Set modification time if specified
            if self._file_mtime is not None:
                os.utime(temp_path, (self._file_mtime, self._file_mtime))
            
            self._local_file_path = temp_path
            
        except Exception as e:
            # Clean up the temporary file on error
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise e
    
    def looks_same(self, other_fpath: str) -> Optional[bool]:
        """
        Provides a quick comparison of this dummy file with another file.
        
        For FileProxyDummy, we can determine if files are the same by comparing
        the version numbers embedded in the file content, without needing to
        materialize first. This is a quick but reliable comparison for dummy files.
        
        Args:
            other_fpath: Path to the other file to compare
            
        Returns:
            Optional[bool]: True if the files are the same, False if different, 
                           or None if unable to determine.
        """
        try:
            if not os.path.exists(other_fpath):
                return False
            
            # Read the other file and check if it has the same version number
            with open(other_fpath, 'r') as f:
                content = f.read()
            
            # Check if it's a dummy file (1KB with version number at start)
            if len(content) != 1024:
                return False
            
            # Extract version number from the other file
            other_version = content.split()[0] if content.split() else ""
            
            # Compare with our version number
            return str(self.version_num) == other_version
                
        except (OSError, IOError, ValueError):
            return None
    
    async def peek_metadata(self) -> Optional[OriginMetadata]:
        """Report the dummy file's cheap metadata for testing body-retention / truncation flows.

        Dummy content is deterministic: the version number padded to 1KB, so size
        is known up front (1024) and mtime is whatever was configured via init_mtime
        or touch(). This makes the dummy useful for exercising truncation and
        change-detection paths without materializing.
        """
        return OriginMetadata(size=1024, mtime=self._file_mtime)

    def retrieval_hint(self) -> Dict[str, Any]:
        return {"source": "dummy", "version_num": self.version_num}

    def _pre_materialize_for_comparison(self, temp_dir: Path) -> Optional[str]:
        """
        Pre-materialize this file for comparison purposes and return the temp file path.
        
        This method creates a temporary file for comparison and immediately cleans it up
        to avoid orphaned files. It's designed for testing scenarios where we need to
        compare files without permanently materializing them.
        
        Args:
            temp_dir: Directory where temporary files should be created
            
        Returns:
            Optional[str]: Path to the temporary file if successful, None otherwise
        """
        if self._materialization_completed and self._local_file_path:
            # Already materialized, return existing path
            return self._local_file_path
        
        try:
            # Create a temporary file for comparison
            filename = os.path.basename(self._ref_path)
            temp_fd, temp_path = tempfile.mkstemp(suffix=os.path.splitext(filename)[1], dir=str(temp_dir))
            
            try:
                # Write version number padded to 1KB
                content = str(self.version_num)
                padded_content = content.ljust(1024, ' ')
                
                with os.fdopen(temp_fd, 'w') as f:
                    f.write(padded_content)
                
                # Set modification time if specified
                if self._file_mtime is not None:
                    os.utime(temp_path, (self._file_mtime, self._file_mtime))
                
                return temp_path
                
            except Exception:
                # Clean up on error
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
                
        except Exception:
            return None

    def cleanup(self) -> None:
        """
        Clean up any temporary files created by this proxy.
        
        This method should be called to ensure proper cleanup of temporary files,
        especially when using orphan_tempfile=True or when tests fail unexpectedly.
        """
        if self._local_file_path and os.path.exists(self._local_file_path):
            try:
                os.remove(self._local_file_path)
            except (OSError, IOError):
                # Ignore errors during cleanup - file might already be removed
                pass
            finally:
                self._local_file_path = None

    def get_context_info(self) -> Dict[str, Any]:
        """Return safe context information for logging/debugging."""
        return {
            "proxy_type": "FileProxyDummy",
            "ref_path": self._ref_path,
            "grouping_key": self.grouping_key,
            "version_num": self.version_num,
            "materialize_secs": self.materialize_secs,
            "allow_pre_materialize_info": self.allow_pre_materialize_info,
            "forced_to_fail_counter": self.forced_to_fail_counter,
            "orphan_tempfile": self.orphan_tempfile,
            "init_mtime": self.init_mtime,
            "local_file_path": self._local_file_path,
            "was_deployed": self._was_deployed,
            "materialization_started": self._materialization_started,
            "materialization_completed": self._materialization_completed,
            "file_mtime": self._file_mtime
        }
