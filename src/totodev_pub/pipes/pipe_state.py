# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from enum import Enum
from pathlib import Path
from typing import Self, Optional

from totodev_pub.pipes.special_pipe_file_nickname import SpecialPipeFileNickname
from totodev_pub.pipes.toto_pipe_begin_data import ToToPipeBeginData

class PipeState(Enum):
    """Represents the possible states of a ToToPipe based on its working directory contents.
    
    States are determined by the presence and state of special files:
    - _pipe_begin.yaml: Indicates initialization
    - _pipe_heartbeat: Indicates active execution
    - _pipe_end.yaml: Indicates completion
    - _pipe_execute_fails.yaml: Indicates execution failures
    
    The state is always inferred from the files present, making it safe for
    cross-process observation of pipe state.
    """
    UNINITIALIZED = "uninitialized"    # No begin file - pipe not properly initialized
    INITIALIZED = "initialized"         # Has begin file, no heartbeat/end - ready to run
    RUNNING = "running"                # Has begin + fresh heartbeat - currently executing
    STALLED = "stalled"               # Has begin + stale heartbeat - was running, may have crashed
    COMPLETED = "completed"           # Has begin + end file - finished successfully
    FAILURES = "failures"            # Has begin + execute fails file - has one or more failures logged
    INVALID = "invalid"

    @classmethod
    def infer_state(cls, working_dir: Path, heartbeat_timeout_secs: Optional[float] = None) -> Self:
        """Infer the state of the pipe from the working directory.

        Uses the SpecialFileNickname.file_ages() method to get the ages/presence of the special files.
        
        The state is determined by the following rules (halting on the first match):
        - No begin file → UNINITIALIZED
        - Has end file + begin file → COMPLETED
        - Has heartbeat file + begin file:
            - Fresh heartbeat → RUNNING
            - Stale heartbeat:
                - Has execute fails file → FAILURES
                - No execute fails file → STALLED
        - Has execute fails file + begin file → FAILURES
        - Has only begin file → INITIALIZED
        - Inconsistent combination → INVALID
        
        Args:
            working_dir: The path to the working directory
            heartbeat_timeout_secs: Optional timeout in seconds for heartbeat staleness. If None,
                                  the timeout will be loaded from the begin file.
            
        Returns:
            PipeState: The inferred state of the pipeline
        """
        # Get ages of all special files (None if file doesn't exist)
        file_ages:dict[SpecialPipeFileNickname,Optional[float]] = SpecialPipeFileNickname.file_ages(working_dir)
        
        # Check for begin file first - required for all valid states except UNINITIALIZED
        if file_ages[SpecialPipeFileNickname.BEGIN] is None:
            # If any other special file exists without begin file -> INVALID
            if any(age is not None for name, age in file_ages.items() if name != SpecialPipeFileNickname.BEGIN):
                return PipeState.INVALID
            return PipeState.UNINITIALIZED
            
        # At this point we know begin file exists
        if file_ages[SpecialPipeFileNickname.COMPLETION] is not None:
            return PipeState.COMPLETED
            
        # Check heartbeat if it exists
        if file_ages[SpecialPipeFileNickname.HEARTBEAT] is not None:
            # Use provided timeout or load from begin file
            timeout = heartbeat_timeout_secs
            if timeout is None:
                # Load timeout from begin file
                begin_file = str(working_dir / SpecialPipeFileNickname.BEGIN.filename())
                begin_data = ToToPipeBeginData.load(begin_file)  # Returns a single instance
                timeout = begin_data.heartbeat_timeout_secs or 60  # Default 60 seconds
            
            # Fresh heartbeat -> RUNNING
            if file_ages[SpecialPipeFileNickname.HEARTBEAT] < timeout:
                return PipeState.RUNNING
            
            # Stale heartbeat - check for execute fails file
            if file_ages[SpecialPipeFileNickname.EXECUTE_FAILS] is not None:
                return PipeState.FAILURES
            return PipeState.STALLED
            
        # Check for execute fails file
        if file_ages[SpecialPipeFileNickname.EXECUTE_FAILS] is not None:
            return PipeState.FAILURES
            
        # Only begin file exists
        return PipeState.INITIALIZED 


    def is_completed(self) -> bool:
        """Returns True if the pipe is completed."""
        return self == PipeState.COMPLETED

    def is_running(self) -> bool:
        """Returns True if the pipe is running."""
        return self == PipeState.RUNNING


    def was_initialized(self) -> bool:
        """Returns True if the pipe was initialized and is not invalid."""
        return self not in (PipeState.UNINITIALIZED, PipeState.INVALID)


