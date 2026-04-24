# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from enum import Enum
from pathlib import Path
from typing import Dict, Optional
import os
import time

class SpecialPipeFileNickname(str, Enum):
    """Enum for special file nicknames used in the pipeline system.
    
    These represent standard files that are used to track pipeline state and metadata:
    - BEGIN: Pipeline initialization data
    - COMPLETION: Pipeline completion marker
    - HEARTBEAT: Active execution indicator
    - EXECUTE_FAILS: Execution failure records
    
    Each nickname has several derived forms:
    1. Base nickname (the enum value itself, e.g. "begin")
    2. Filename (e.g. "_pipe_begin.yaml")
    3. Constant form (e.g. BEGIN)
    4. Absolute path (when combined with working_dir)
    """
    BEGIN = "begin"
    COMPLETION = "completion"
    HEARTBEAT = "heartbeat"
    EXECUTE_FAILS = "execute_fails"

    def filename(self) -> str:
        """Get the filename for this special file.
        
        Returns:
            str: The filename with appropriate prefix and extension
        """
        # Special case for heartbeat which uses .txt
        if self == SpecialPipeFileNickname.HEARTBEAT:
            return f"_pipe_{self.value}.txt"
        return f"_pipe_{self.value}.yaml"

    def abspath(self, working_dir: str | Path) -> Path:
        """Get the absolute path for this special file.
        
        Args:
            working_dir: The working directory where the special file is located
            
        Returns:
            Path: The absolute path to the special file
        """
        return Path(working_dir) / self.filename()

    @classmethod
    def file_ages(cls, working_dir: Path) -> Dict[str, Optional[float]]:
        """Get the ages of all special files in seconds.
        
        Uses os.scandir() to efficiently get file information in a single system call.
        The DirEntry objects from scandir cache stat information when calling is_file(),
        allowing us to get both directory contents and stat info in one system call.
        
        Args:
            working_dir: Directory to check for special files
            
        Returns:
            Dictionary mapping enum values to file ages in seconds.
            Age is None if file doesn't exist.
            
        Raises:
            OSError: If there are filesystem-related errors (permissions, missing directory, etc.)
        """
        current_time = time.time()
        ages = {nickname: None for nickname in cls}  # Initialize all ages to None
        
        # Create a mapping of filenames to nicknames for quick lookup
        filename_to_nickname = {nickname.filename(): nickname for nickname in cls}
        
        # Get all file information in one system call
        with os.scandir(working_dir) as entries:
            for entry in entries:
                # Only process files we care about
                if entry.name in filename_to_nickname and entry.is_file():
                    # is_file() caches the stat information
                    # stat(follow_symlinks=False) uses the cached information
                    nickname = filename_to_nickname[entry.name]
                    ages[nickname] = current_time - entry.stat(follow_symlinks=False).st_mtime
        
        return ages 