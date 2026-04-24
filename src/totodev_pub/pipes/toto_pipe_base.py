# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Base class for all toto pipes.

A toto pipe is a class that represents a task that can be executed by Luigi.
It has a working directory where all input and output files are stored.
The working directory contains several special files:
- _pipe_begin.yaml: Contains information about the pipe's inputs and configuration
- _pipe_completion.yaml: Indicates completion
- _pipe_heartbeat.txt: Updated periodically to indicate the pipe is still running
- _pipe_execute_fails.yaml: Records any exceptions that occur during execution

The pipe's inputs and outputs are described by a ToToPipeTypeInfo object.
This object maps nicknames to file patterns and model classes.
The model classes must be subclasses of FileMappedPydanticMixin.

Provides utility methods for preparing the working directory including:
- Serializing input data objects into files
- Loading private configs from a file (e.g. passwords, API keys, etc)
- Checking that all expected input files exist
- Providing utility methods for retrieving files by nickname

If your process is quick or small, don't use this stuff.  It's got lots of overhead.
The primary case for this are tasks with longer runtimes or with unreliable execution.
Examples:
- ETL to load a database
- Complex LLM interactions

NOTE: When using this class, do implement an execute() method rather than overriding run().
"""

import asyncio
from pathlib import Path
from typing import Dict, List, Type, Any, Mapping, Generator, Optional, Union, Literal,Self
from types import MappingProxyType
import inspect
from datetime import datetime
import multiprocessing
import shutil
import time
import luigi
from pydantic import BaseModel
from enum import Enum
from abc import ABC, abstractmethod
import traceback

from .rel_fpath_pattern import RelativeFilepathPattern 
from totodev_pub.pipes.app_config_cached_subset import AppConfigCachedSubset
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.pipes.toto_pipe_begin_data import ToToPipeBeginData
from totodev_pub.pipes.toto_pipe_completion_data import ToToPipeCompletionData, TimingInfo
from totodev_pub.pipes.toto_pipe_execute_fails_data import ToToPipeExecuteFailsData, ExceptionInfo
from totodev_pub.pipes.toto_pipe_type_info import ( ToToPipeTypeInfo, SPECIAL_NICKNAMES,)
from totodev_pub.pipes.special_pipe_file_nickname import SpecialPipeFileNickname
from totodev_pub.pipes.pipe_state import PipeState


def _luigi_build_in_child_process(task: "ToToPipeBase") -> None:
    """Run Luigi with a local scheduler in a child process main thread (Worker uses OS signals)."""
    luigi.build([task], local_scheduler=True)


class ToToPipeBase(luigi.Task):
    """Base class for long running luigi tasks that are bound to a specific working directory.

    Instances of this class can have potentially two roles during their lifetime:
    
    This base class with convenience methods for
    1. Setup and monitoring of a task via its "working_directory"
        - moving data and config info into the working directory
        - viewing the status of the task via the heartbeat file
        - retrieving files from the working directory via declared "nicknames"
    2. Actually performing the work as a Luigi Task that has been invoked with run()
    
    Derived classes should implement:
    - pipe_type_info()  - classmethod describing the inputs, outputs, and other metadata
    - execute()  - much the same as you would typically implement run() for a luigi task
    - outputs()  - much the same as you would typically implement outputs() for a luigi task
    - OPTIONALLY: 
        - bind_inputs() to add default inputs and params

    Steps to use for running:
        1. Callers should create an instance pointing to an existing working directory.
        2. Call bind_inputs() with the expected inputs and private configs.
        3. Call run() to execute the task.

    Steps to use for accessing files and params:
        1. Call access_pipe() with the working directory (requires that register_pipe_class() was called in this process-lifetime)
        2. Use list_files() to get file paths or load_files() to get deserialized Pydantic models.

    Note: Do not override run(), instead implement execute().
          Derived classes are encouraged to create their own bind() method to encapsulate and simplify the call to bind_inputs().
          In the event of an error, you may want to call purge_outputs() to clean up any partially written files.
          As a code structure/style matter, it is recommended that you use supporting classes to reduce the amount of code in your actual pipe class.
          State management is handled by the base class. Simply finishing the execute() method without exception will mark the pipe as completed.
          
    """

    working_dir = luigi.Parameter(
        description="Root directory path where all input/output/intermediate files will be stored",
    )
    
    private_configs = luigi.OptionalParameter(
        default=None,
        description="""Optional path to a YAML or JSON file containing private application configuration.
        Expected to be a serialized AppConfigCachedSubset object.  This file will hold a snapshot of the private configs values.
        If this file is not provided, the private configs will not be available to the pipe.
        This file is intended to be shared across other trusted pipes and allows us to avoid serializing confidential info in many places.

        If relative filename, it is relative to the working directory (e.g. '../../pipe_private_configs.json').
        """
    )

    _pipe_class_registry: Dict[str, Type[Self]] = {} # maps class name to class type


    def __init__(self, *args, **kwargs) -> None:
        """Initialize the task with a working directory and validate it exists.
        
        Expects: 
            working_dir - the path to the working directory
            private_configs - the path to the private configs file (optional)
        
        The subfolder structure implied by the pipe_type_info's inputs and possible_outputs
        is affirmed (meaning expected directories are created if they don't exist).
        A private_configs file is only necessary if the class's type info has private_configs set.
        
        Args:
            *args: Positional arguments passed to luigi.Task
            **kwargs: Keyword arguments passed to luigi.Task
        
        Raises:
            ValueError: If working_dir does not exist
            ValueError: If private_app_config_cached is provided but file doesn't exist
            ValueError: If private_app_config_cached is provided but not a YAML/JSON file
            ValueError: If _pipe_begin file exists but was created by a different pipe class
            NotImplementedError: If the class overrides run() instead of implementing execute()

        NOTE: This method does not create or modify any files, only empty directories (if needed).
        """
        # Check if subclass accidentally implemented run() instead of execute()
        if (self.__class__ != ToToPipeBase and 
            not inspect.isabstract(self.__class__) and
            'run' in self.__class__.__dict__):
            raise NotImplementedError(
                f"Task {self.__class__.__name__} incorrectly implements run() instead of execute().\n"
                "ToToPipeBase subclasses must implement execute() and should never override run().\n"
                "Please rename your run() method to execute() to fix this error."
            )

        super().__init__(*args, **kwargs) # Initialize luigi task

        # Initialize private configs to None - will be loaded on demand during execute()
        self._private_configs: MappingProxyType = None

        # get the pipe type info from our derived class.
        self._pti: ToToPipeTypeInfo = self.__class__.pipe_type_info() #cache the pipe type info
        self._heartbeat_file = self.resolve_nickname(SpecialPipeFileNickname.HEARTBEAT)

        if not Path(self.working_dir).exists():
            raise ValueError(f"Working directory must exist: {self.working_dir}")
            

        self._begin_params: Optional[Mapping[str, Any]] = None
        self._is_executing: bool = False
        self._completion_data: Optional[ToToPipeCompletionData] = None

    @classmethod
    def register_pipe_class(cls, pipe_class: Type['ToToPipeBase']) -> None:
        """Register a derived pipe class with the class registry.
        
        Not required.  Necessary only if you want to use access_pipe() to dynamically load a pipe class from a working directory.
        
        Args:
            pipe_class: The derived pipe class to register. 
            Must be a subclass of ToToPipeBase.
        """
        if not issubclass(pipe_class, cls):
            raise ValueError(f"Cannot register non-pipe class: {pipe_class}")
        cls._pipe_class_registry[pipe_class.__name__] = pipe_class

    @classmethod
    def registered_pipe_classes(cls) -> Dict[str, Type[Self]]:
        """Returns a list of all registered pipe classes.  
        
        see register_pipe_class() for more information.
        """
        return cls._pipe_class_registry

    @classmethod
    def access_pipe(cls, working_dir: str | Path, private_configs_file: Optional[str | Path] = None) -> 'ToToPipeBase':
        """Load the pipe class from the registry and return an instance.

        Infers the class from the _pipe_begin.yaml file in the working directory.
        Cannot access the private_configs info (deliberately) unless you provide it.
        
        Args:
            working_dir: The working directory previously initialized by the pipe class.
            private_configs_file: Optional.  see constructor for explanation.  Only needed for run() to work.
        """
        # Get the class name from the _pipe_begin.yaml file
        begin_file = Path(working_dir) / SpecialPipeFileNickname.BEGIN.filename()
        if not begin_file.exists():
            raise ValueError(f"Pipe begin file not found at {begin_file} , suggesting that ToToPipeBase.bind_inputs() was not called on this directory.")
            
        # Create a dummy instance to load the begin file
        dummy = cls(working_dir=str(working_dir), private_configs=private_configs_file)
        begin_obj = dummy.load_files(SpecialPipeFileNickname.BEGIN)
        if not begin_obj:
            raise RuntimeError("Begin file not found or could not be loaded")
        class_name = begin_obj[0].task_classname
        
        if (class_obj := cls._pipe_class_registry.get(class_name,None)) is None:
            raise ValueError(f"Pipe class name {class_name} not found in registry.  This typically means that class wasn't registered with ToToPipeBase.register_pipe_class() in this process-lifetime.")
        return class_obj(working_dir=working_dir, private_configs=private_configs_file)
            


    def _private_configs_path(self) -> Optional[Path]:
        """Returns validated absolute path to private configs file.
        
        Returns:
            Absolute Path to the config file if private_configs is set, None otherwise.
            
        Raises:
            ValueError: If config file path is within working directory
            ValueError: If config file is not a YAML/JSON file
        """
        if not self.private_configs:
            return None
            
        config_path = Path(self.private_configs)
        abs_path = config_path if config_path.is_absolute() else Path(self.working_dir) / config_path
        
        if Path(self.working_dir).resolve() in abs_path.resolve().parents:
            raise ValueError(f"Private config file must not be at or below working directory: {self.private_configs}")
            
        if not abs_path.suffix.lower() in ['.yaml', '.yml', '.json']:
            raise ValueError(f"Private config file must be YAML or JSON: {self.private_configs}")
            
        return abs_path

    @classmethod
    def _update_heartbeat(cls, working_dir: str | Path, new_txt: Optional[str] = None, timeout_secs: Optional[float] = None) -> bool:
        """Tests whether the heartbeat file has been modified within the given timeout.

        This is an internal method for managing this pipe instance's heartbeat file.
        Used to update the heartbeat file or check if this pipe instance is still running.
        
        Note that the heartbeat file is created when run() starts and is deleted when run() ends.
        
        Args:
            working_dir: Path to the working directory where the file would be located
            new_txt: Optional new text to set the heartbeat to.
            timeout_secs: Timeout in seconds. If None, uses the pipe type info's timeout value.
                        If that is also None, defaults to 60 seconds.

        Returns:
            If new_txt is provided: True if the file was updated, False otherwise.
            If new_txt is not provided: True if the heartbeat file exists and is recent, False otherwise.
            
        Raises:
            RuntimeError: If the heartbeat file doesn't exist and no new_txt is provided.
        """
        working_dir = Path(working_dir)
        heartbeat_file = working_dir / SpecialPipeFileNickname.HEARTBEAT.filename()
        if not (new_txt or heartbeat_file.exists()):
            raise RuntimeError(f"Heartbeat file not found: {heartbeat_file}. This typically means the pipe is not running.")
        if new_txt:
            heartbeat_file.write_text(new_txt)
            # No need to clear state cache since this is a class method
            return True
        if timeout_secs is None:
            # Create a dummy instance to get the timeout value
            dummy = cls(working_dir=str(working_dir))
            timeout_secs = dummy._pti.heartbeat_timeout_secs or 60
        return heartbeat_file.stat().st_mtime > time.time() - timeout_secs

    @classmethod
    def get_completion_data(cls, working_dir: str | Path) -> Optional[ToToPipeCompletionData]:
        """Get the completion data if it exists, otherwise return None.

        This is a non-blocking, non-async version that returns immediately.
        For a blocking async version that waits for completion, see wait_for_completion().

        From within the execute() method:
            It will return the planned completion data rather than the actual completion data.
            Can be used with set_completion_data() to set the actual completion data.

        Args:
            working_dir: The working directory to check for completion data.

        Returns:
            The ToToPipeCompletionData object if the completion file exists, None otherwise.
        """
        if cls.infer_state(working_dir) != PipeState.COMPLETED:
            return None
        dummy = cls(working_dir=str(working_dir))
        return dummy.load_files(SpecialPipeFileNickname.COMPLETION)[0]

    @classmethod
    async def wait_for_completion(cls, working_dir: str | Path, timeout_secs: float = 0, allow_start_secs: float = 2, polling_interval: Optional[float] = None) -> Optional[ToToPipeCompletionData]:
        """Awaits completion and returns the completion data object.

        This is a blocking async version that waits for completion.
        For a non-blocking, non-async version that returns immediately, see get_completion_data().

        Returns immediately if already completed. Otherwise sleeps/awakens to check changes.
        Has the option of allowing a grace period for the heartbeat file to appear.

        If timeout_secs is 0, it uses "patient" mode, and watches the heartbeat file until it is no longer recent.
        If timeout_secs is non-zero, it uses "strict" mode, and watches the end file until it appears or the timeout is reached.
        Heartbeat recency is judged against the heartbeat_timeout_secs value in the _pipe_begin.yaml file.

        Args:
            working_dir: Path to the working directory where the file would be located
            timeout_secs: Timeout in seconds. If None, uses the pipe type info's timeout value
            allow_start_secs: If the heartbeat file doesn't exist, wait up to this many seconds for it to appear.
            polling_interval: If present, controls how often to try to awaken and check the end file and heartbeat file.

        Returns:
            The completion data object loaded from the end file.
            None if the timeout is reached and completion has not occurred.

        Raises:
            RuntimeError: If the heartbeat file doesn't exist by the time the allow_start_secs is reached.
            ValueError: If working_dir doesn't exist
            ValueError: If polling_interval is not positive
            ValueError: If timeout_secs is negative
        """
        working_dir = Path(working_dir)
        if not working_dir.exists():
            raise ValueError(f"Working directory must exist: {working_dir}")

        # Get paths to special files
        end_file = working_dir / SpecialPipeFileNickname.COMPLETION.filename()

        # Create a dummy instance only for loading files (needed for model deserialization)
        dummy = cls(working_dir=str(working_dir))

        # Check if end file already exists
        if end_file.exists():
            try:
                return dummy.load_files(SpecialPipeFileNickname.COMPLETION)[0]
            except Exception as e:
                raise RuntimeError(f"Failed to load existing end file: {end_file}") from e

        # Check if polling_interval is positive
        if polling_interval is not None and polling_interval <= 0:
            raise ValueError("polling_interval must be positive")

        if timeout_secs < 0:
            raise ValueError("timeout_secs must be non-negative")

        # Failed run: _pipe_execute_fails.yaml exists and there is no completion file. The
        # heartbeat is already removed; do not require it (avoids spurious errors when the
        # caller runs after a quick failure or synchronous luigi.build).
        if PipeState.infer_state(working_dir) == PipeState.FAILURES:
            return None

        # Wait for heartbeat file to appear within allow_start_secs
        start_wait_until = time.time() + allow_start_secs
        while time.time() < start_wait_until:
            try:
                if cls._update_heartbeat(str(working_dir)):  # This will raise RuntimeError if file doesn't exist
                    break
            except RuntimeError:
                await asyncio.sleep(min(0.1, polling_interval or 0.1))  # Short sleep while waiting for heartbeat file
            except Exception as e:
                raise RuntimeError(f"Unexpected error checking heartbeat file: {e}") from e
        else:
            raise RuntimeError(f"Heartbeat file did not appear within {allow_start_secs} seconds")

        # Patient mode: Watch heartbeat file until it's no longer recent
        if timeout_secs == 0:
            while True:
                # First check for completion
                if end_file.exists():
                    try:
                        return dummy.load_files(SpecialPipeFileNickname.COMPLETION)[0]
                    except Exception as e:
                        raise RuntimeError(f"Failed to load end file: {end_file}") from e

                # Then check heartbeat
                try:
                    if not cls._update_heartbeat(str(working_dir)):  # Heartbeat file is no longer recent
                        # One final check for completion before returning None
                        if end_file.exists():
                            try:
                                return dummy.load_files(SpecialPipeFileNickname.COMPLETION)[0]
                            except Exception as e:
                                raise RuntimeError(f"Failed to load end file: {end_file}") from e
                        return None
                except RuntimeError:  # Heartbeat file doesn't exist
                    # One final check for completion before returning None
                    if end_file.exists():
                        try:
                            return dummy.load_files(SpecialPipeFileNickname.COMPLETION)[0]
                        except Exception as e:
                            raise RuntimeError(f"Failed to load end file: {end_file}") from e
                    return None
                except Exception as e:
                    raise RuntimeError(f"Unexpected error checking heartbeat file: {e}") from e
                await asyncio.sleep(polling_interval or 1.0)

        # Strict mode: Watch for end file until timeout
        timeout_at = time.time() + timeout_secs
        while time.time() < timeout_at:
            if end_file.exists():
                try:
                    return dummy.load_files(SpecialPipeFileNickname.COMPLETION)[0]
                except Exception as e:
                    raise RuntimeError(f"Failed to load end file: {end_file}") from e
            await asyncio.sleep(polling_interval or 1.0)

        # One final check before giving up
        if end_file.exists():
            try:
                return dummy.load_files(SpecialPipeFileNickname.COMPLETION)[0]
            except Exception as e:
                raise RuntimeError(f"Failed to load end file: {end_file}") from e
        return None


    def spawn(self, mode: str = "in-process") -> None:
        """Spawn a new process to run this pipe.
        
        This method is non-blocking - it will return immediately after starting the run.
        For ``mode="in-process"``, Luigi runs in a **child process** (not the caller thread) so
        the Luigi Worker can use OS signals and callers can use ``wait_for_completion()`` while
        the heartbeat file still exists. (A synchronous ``luigi.build()`` in the caller would
        finish before ``wait_for_completion()`` runs, which breaks the heartbeat-based wait on
        failure paths.)

        The pipe will transition to RUNNING state once execution starts.
        
        Args:
            mode: How to run the task. One of:
                - "in-process": Luigi local scheduler in a dedicated child process
                - "luigid": Run through a central scheduler (luigid)
                
        Raises:
            RuntimeError: If the pipe is already completed or setup is incomplete
        """
        # Check if we're in a valid state to run
        state = self.get_state(cache_state_secs=0)
        if state == PipeState.COMPLETED:
            raise RuntimeError(
                f"Cannot run pipe in directory '{self.working_dir}' as it has already completed.\n"
                "Please use a different working directory or purge the outputs first."
            )
        if state != PipeState.INITIALIZED:
            raise RuntimeError(
                f"Cannot run pipe in directory '{self.working_dir}' as setup is incomplete.\n"
                f"The pipe is in {state} state. This typically means bind_inputs() was not called.\n"
                "Please call bind_inputs() with appropriate parameters before running the pipe."
            )

        # Run the task
        if mode == "in-process":
            # Must not run luigi.build on a background thread: Luigi Worker registers SIGUSR1.
            self._luigi_build_process = multiprocessing.Process(
                target=_luigi_build_in_child_process,
                args=(self,),
                name=f"totopipe-luigi-local-{self.__class__.__name__}",
                daemon=False,
            )
            self._luigi_build_process.start()
        elif mode == "luigid":
            # Run through a central scheduler
            luigi.build([self])
        else:
            raise ValueError(f"Invalid mode: {mode}. Must be one of: 'in-process', 'luigid'")

        # Wait for the heartbeat file to appear (up to 5 seconds)
        start_time = time.time()
        while time.time() - start_time < 5:
            if self.get_state(cache_state_secs=0) == PipeState.RUNNING:
                break
            time.sleep(0.1)

    def run(self):
        """Execute the task.
        
        This method is called by Luigi to run the task. It handles:
        1. Loading private configs if needed
        2. Setting up the heartbeat file
        3. Calling execute() to do the actual work
        4. Saving completion data
        5. Cleaning up the heartbeat file
        
        Do not override this method. Instead, implement execute().
        """
        # Check if we're in a valid state to run
        state = self.get_state()
        if state != PipeState.INITIALIZED:
            raise RuntimeError(
                f"Cannot run pipe in directory '{self.working_dir}' as setup is incomplete.\n"
                f"The pipe is in {state} state. This typically means bind_inputs() was not called.\n"
                "Please call bind_inputs() with appropriate parameters before running the pipe."
            )

        # Check if subclass accidentally implemented run() instead of execute()
        if (self.__class__ != ToToPipeBase and 
            not inspect.isabstract(self.__class__) and
            'run' in self.__class__.__dict__):
            raise NotImplementedError(
                f"Task {self.__class__.__name__} incorrectly implements run() instead of execute().\n"
                "ToToPipeBase subclasses must implement execute() and should never override run().\n"
                "Please rename your run() method to execute() to fix this error."
            )

        # Record start time
        started_time = datetime.now()

        # Load the run params from the begin file
        begin_data_list = self.load_files(SpecialPipeFileNickname.BEGIN)
        if not begin_data_list:
            raise RuntimeError("Begin file not found or could not be loaded")
        begin_data = begin_data_list[0]  # We know there's only one BEGIN file
        self._begin_params = MappingProxyType(begin_data.params)  # immutable view of the params

        # Create heartbeat file with current timestamp
        heartbeat_file = self.resolve_nickname(SpecialPipeFileNickname.HEARTBEAT)
        self._update_heartbeat(self.working_dir, new_txt=str(time.time()))
        self._clear_state_cache()  # Force state recalculation

        # Set flag to allow access to private configs and params
        self._is_executing = True

        try:
            # Initialize completion data with both start and end time
            self._completion_data = ToToPipeCompletionData(
                timing=TimingInfo(
                    started_time=started_time,
                    ended_time=started_time  # Initialize with start time, will update later
                ),
                outputs={},
                extra={}
            )

            # Execute the task
            self.execute()

            # Update completion data timing with actual end time
            self.completion_data.timing.ended_time = datetime.now()

            # Update outputs information
            outputs: Dict[str, List[str]] = {}
            for nickname, (pattern, _) in self._pti.outputs.items():
                matched_files = pattern.matched_files(self.working_dir)
                if matched_files:  # Only include outputs that have matching files
                    outputs[nickname] = [str(Path(f).relative_to(self.working_dir)) for f in matched_files]
            self.completion_data.outputs = outputs

            # Save completion data
            completion_file = self.resolve_nickname(SpecialPipeFileNickname.COMPLETION)
            self.completion_data.save(str(completion_file))

            # Clean up heartbeat file
            heartbeat_file.unlink()

            # Clear state cache to force recalculation
            self._clear_state_cache()

        except Exception as e:
            # Save exception data
            self._save_exception_data(e)

            # Clean up heartbeat file
            if heartbeat_file.exists():
                heartbeat_file.unlink()

            # Re-raise the exception
            raise e

        finally:
            # Reset flags and clear completion data
            self._is_executing = False
            self._completion_data = None

    def execute(self) -> None:
        """Execute the pipe.

        This method should be implemented by derived classes to perform the actual work.
        To store additional data in the completion file, use the completion_data property.

        Example:
            def execute(self):
                # Do some work
                result = process_data()
                
                # Store additional data in completion file
                self.completion_data.extra["process_result"] = result
                self.completion_data.extra["processed_at"] = datetime.now()

        Raises:
            RuntimeError: If the pipe has already completed (completion data file exists)
            RuntimeError: If bind_inputs() has not been called (begin data file does not exist)
            RuntimeError: If execute() is called recursively

        Note:
            - Return values from this method are ignored as they are not compatible with Luigi.
            - If your process encounters an error that should prevent completion,
              raise an Exception. The completion file will not be written if an
              exception is raised.
            - To store data for later retrieval, use self.completion_data.extra
        """
        raise NotImplementedError("Subclasses must implement execute()")

    @classmethod
    def pipe_type_info(cls) -> ToToPipeTypeInfo:
        """Get the pipe type info for this class.

        Derived classes MUST implement this method to explain their inputs, outputs, and private configs.
        The base class returns an empty ToToPipeTypeInfo.
        
        This method is the single source of truth for pipe configuration, containing:
        - inputs: Dict mapping patterns to model classes for input files
        - possible_outputs: Dict mapping patterns to model classes for output files
        - required_private_configs: List of required private config keys
        """
        if cls == ToToPipeBase:
            # Return empty type info for base class
            return ToToPipeTypeInfo(inputs={}, outputs={}, private_cfgs=[])
        raise NotImplementedError(
            f"Class {cls.__name__} must implement pipe_type_info() to define its inputs and outputs.\n"
            "This method should return a ToToPipeTypeInfo object describing the pipe's configuration."
        )


    def _cleanup_failed_bind(self) -> None:
        """Clean up after a failed bind_inputs() call by removing the begin file.
        
        This is an internal method intended to be used by derived classes in their bind_inputs() 
        exception handlers to ensure the pipe returns to an uninitialized state if their additional 
        validation or setup fails after calling super().bind_inputs().
        
        This method:
        - Removes only the begin file (_pipe_begin.yaml)
        - Clears the state cache to force recalculation
        - Returns the pipe to an uninitialized state
        - Preserves any other files in the working directory
        """
        begin_file = self.resolve_nickname(SpecialPipeFileNickname.BEGIN)
        if begin_file.exists():
            begin_file.unlink()
        self._clear_state_cache()

    def _map_source_to_destination_filenames(self, 
                                            source_files: List[Union[str, Path]], 
                                            pattern: RelativeFilepathPattern) -> List[Path]:
        """Map source filenames to destination filenames based on a pattern.
        
        Method attempts to preserve the original file basename and extension.
        May append numeric digits to achieve uniqueness if the base name is repeated.
        
        Args:
            source_files: List of source file paths (as strings or Path objects)
            pattern: The pattern to use for destination filenames
            
        Returns:
            List of destination paths, one for each source file
            
        Raises:
            ValueError: If pattern has no wildcards
            ValueError: If a destination filename doesn't match the pattern
        """
        if not pattern.has_wildcards:
            raise ValueError(f"Pattern '{pattern.pattern}' must contain wildcards to handle multiple files")
            
        # Calculate destination directory once
        dest_dir = pattern.calc_path(dir_only=True, root_folder=self.working_dir)
        
        # First attempt: try to preserve original filenames
        dest_files = []
        for src_file in source_files:
            # Convert to Path if needed and preserve original filename and extension
            src_path = Path(src_file)
            dest_path = dest_dir / src_path.name
            dest_files.append(dest_path)
        
        # Check for duplicates and add suffixes if needed
        seen_names = {}
        for i, dest_path in enumerate(dest_files):
            if dest_path in seen_names:
                # Get the base name and extension separately
                src_path = Path(source_files[i])
                base_name = src_path.stem
                extension = src_path.suffix  # Keep original extension
                
                # Add a three-digit suffix to the base name
                new_name = f"{base_name}{seen_names[dest_path]:03d}{extension}"
                seen_names[dest_path] += 1
                
                # Create new destination path with suffix
                dest_files[i] = dest_dir / new_name
            else:
                seen_names[dest_path] = 1
                
        # Verify all destination paths match the pattern
        for dest_path in dest_files:
            if not pattern.is_match(dest_path, root_folder=self.working_dir):
                raise ValueError(f"Generated destination path '{dest_path}' does not match pattern '{pattern.pattern}'")
                
        return dest_files

    def _handle_single_input(self, item: Union[str, Path, FileMappedPydanticMixin], pattern: RelativeFilepathPattern, index: Optional[int] = None) -> None:
        """Handle a single input item, either copying a file or serializing a Pydantic model.
        
        Args:
            item: The input item to handle (file path or Pydantic model)
            pattern: The pattern to use for destination path
            index: Optional index for list items to create unique paths
            
        Raises:
            FileNotFoundError: If a file path is provided but the file doesn't exist
            ValueError: If the input type is invalid
        """
        # Calculate merge components based on pattern and index
        merge_components = []
        if pattern.has_wildcards:
            if index is not None:
                merge_components = [f"{index:04d}"]  # Pad to 4 digits with leading zeros
            else:
                # If pattern has wildcards but no index, use a default value
                merge_components = ["0000"]  # Use 4-digit zero padding

        if isinstance(item, FileMappedPydanticMixin):
            # Serialize object to working directory
            path = pattern.calc_path(merge_components, root_folder=self.working_dir)
            item.save(str(path))
        elif isinstance(item, (str, Path)):
            # Copy file to working directory
            src_path = Path(item).resolve()  # Use resolve() to get absolute path
            if not src_path.exists():
                raise FileNotFoundError(f"Input file not found: {src_path}")
                
            dst_path = pattern.calc_path(merge_components, root_folder=self.working_dir)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Only copy if source and destination are different files
            if not dst_path.exists() or not src_path.samefile(dst_path):
                shutil.copy2(src_path, dst_path)
        else:
            raise ValueError(f"Invalid input type: {type(item)}, expecting filename or FileMappedPydanticMixin")

    def bind_inputs(self, 
                   explicit_inputs: Dict[str, str | FileMappedPydanticMixin | List[str | FileMappedPydanticMixin]], 
                   private_configs_src: Optional[Mapping[str, Any]] = None,
                   params: Optional[Dict[str, Any]] = None,
                   ignore_missing_inputs: bool = False
                   ) -> None:
        """
        Prepares this object for execution by:
        - Creating all necessary subdirectories for input and output patterns
        - Copying input files into the working directory (if file paths are provided)
        - Serializing any data objects into the working directory (if pydantic objects are provided)
        - Loading private configs from a file (optional)
        - Passing in "bind_params" (accessible during run as get_bind_params())

        Args:
            explicit_inputs: Dictionary mapping file nicknames to either:
                           - File paths as strings (files will be copied to working directory)
                           - Pydantic data objects (will be serialized to working directory)
                           - Lists of either of the above
                           Note: When copying files, file metadata (creation/modification times) is preserved.
            private_configs_src: dict-like object to take retrieve values from for storage in cachefile. Possibly unnecessary if passed to constructor.
            params: Dictionary mapping param names to values.
            ignore_missing_inputs: If True, do not raise an error if some inputs are missing.

        Raises:
            ValueError: If attempting to provide explicit inputs for a pattern marked as non-deserializable (model class is None)
            ValueError: If attempting to bind inputs to a completed pipe
            FileNotFoundError: If a provided file path does not exist
        """
        # Check if pipe is already completed
        state = self.get_state()
        if state == PipeState.COMPLETED:
            raise ValueError(f"Cannot bind inputs to pipe in directory '{self.working_dir}' as it is in {state} state")

        # Create subdirectories for input and output patterns
        self._pti.create_pattern_subdirs(self.working_dir)

        # Handle private configs if we have all necessary components
        if private_configs_src:
            self._cache_private_configs(
                self._private_configs_path(),
                private_configs_src,
                self.__class__.pipe_type_info().required_private_configs
            )

        # confirm that the input files either exist or are provided explicitly as serializable objects
        missing_patterns: list[RelativeFilepathPattern] = []
        for nickname, (pattern, model_class) in self._pti.inputs.items():
            if nickname in explicit_inputs:
                input_value = explicit_inputs[nickname]
                # Check if this pattern is marked as non-deserializable
                if model_class is None and isinstance(input_value, FileMappedPydanticMixin):
                    raise ValueError(f"We were passed an object for nickname '{nickname}' but pipe type info does not specify a class (model class is None)")
                
                if (isinstance(input_value, list) and 
                    len(input_value) > 0 and 
                    all(isinstance(x, (str, Path)) for x in input_value)):
                    output_filenames = self._map_source_to_destination_filenames(input_value, pattern)
                    # copy the input files to the output filenames
                    for src_path, dst_path in zip(input_value, output_filenames):
                        shutil.copy2(src_path, dst_path)
                else:
                    # normalize the input to a list for consistent handling
                    items = input_value if isinstance(input_value, list) else [input_value]
                    for i, item in enumerate(items):
                        self._handle_single_input(item, pattern, i if len(items) > 1 else None)
            else:
                # Check if input file exists in working directory
                if not pattern.matched_files(self.working_dir):
                    missing_patterns.append(pattern)

        # Raise error if any required inputs are missing
        if missing_patterns and not ignore_missing_inputs:
            raise ValueError(
                f"Missing required input files for patterns: {[str(p) for p in missing_patterns]}\n"
                f"Working directory: {self.working_dir}"
            )

        # Save begin data
        self._save_begin_data(params)
        self._clear_state_cache()  # Clear cache since we created begin file

    def list_files(self, nickname: str, abs_path: bool = True) -> list[Path]:
        """Finds and returns a list of existing file paths for the given nickname, alpha sorted.

        Relies upon definitions in inputs() and possible_outputs().
        If no such file exists or if the nickname doesn't exist, an empty list is returned.
        See resolve_nickname() to get the filepath of a file that doesn't yet exist.

        This is the preferred method when you need to access file paths without loading their contents.
        For loading file contents into Pydantic models, use load_files() instead.

        Args:
            nickname: The nickname of the file to retrieve
            abs_path: If True, the file paths are returned as absolute paths (else relative to the working directory)

        Returns:
            List of Path objects, sorted alphabetically.
            Empty list if no matching files found or if nickname doesn't exist.
        """
        try:
            paths = self._pti.files(self.working_dir, nickname, load_to_memory=False, abs_path=True)
            if not abs_path:
                # Convert absolute paths to relative paths using Path.relative_to()
                paths = [p.relative_to(self.working_dir) for p in paths]
            return paths
        except ValueError:
            # Return empty list for unknown nicknames
            return []

    def load_files(self, nickname: str) -> list[BaseModel]:
        """Find and deserialize files matching the given nickname's pattern.
        
        Each nickname maps to a file pattern (glob) that may match zero, one, or many files.
        For example:
        - A nickname mapped to "data.json" will return a list of 0 or 1 items
        - A nickname mapped to "data/*.json" may return a list of many items
        - Special nicknames like BEGIN will typically match 0 or 1 files
        
        Args:
            nickname: The nickname to load files for
            
        Returns:
            List of deserialized Pydantic models, one for each matching file.
            Returns empty list if no files found or if nickname unknown.
            
        Raises:
            ValueError: If the nickname's model class is None (non-deserializable)
            ValueError: If the nickname's model class doesn't inherit from FileMappedPydanticMixin
        """
        return self.pipe_type_info().files(self.working_dir, nickname, load_to_memory=True)

    def resolve_nickname(self, nickname: str, abs_path: bool = True, merge: Optional[str] = None) -> Path:
        """Resolves a nickname to its expected filepath.

        Relies upon definitions in inputs() and possible_outputs().
        If merge is provided, it is substituted for the wildcard in the pattern.
        For example, if the pattern is "pdf_file_*.pdf" and merge is "001", the filepath will be "pdf_file_001.pdf".

        Args:
            nickname: The nickname of the pattern to use
            abs_path: If True, return an absolute path, otherwise return a relative path
            merge: Optional string to replace wildcards in the pattern

        Returns:
            Path object pointing to the target file

        Note that in many cases you may actually want the list_files() method instead.
        This method returns a filename regardless of whether it exists or not.
        """
        path = self._pti.calc_filepath(self.working_dir, nickname, merge_str=merge)
        if not abs_path:
            path = Path(str(path).replace(str(self.working_dir) + '/', ''))
        return path
    
    def get_private_configs(self) -> MappingProxyType[str, Any]:
        """Get the private configs that were passed to bind_inputs().

        Essentialy this is a dict of the private configs cached when bind_inputs() was called.
        
        This method is only accessible within the execute() method.
        """
        if not self._is_executing:
            raise ValueError("get_private_configs() is only accessible within the execute() method. Retrieve using load_files() with private configs nickname if needed at other times.")
        # Return cached configs if available (including memory-only case)
        if self._private_configs:
            return self._private_configs
        # Try to load from file if path was specified
        pcp = self._private_configs_path()  
        if not pcp:
            raise ValueError("Cannot call get_private_configs() as no private configs path passed on construction of the pipe.")
        self._private_configs = AppConfigCachedSubset.load(pcp,stability_secs=0.05)
        return self._private_configs

    def get_bind_params(self) -> MappingProxyType[str, Any]:
        """Get the parameters that were passed to bind_inputs().

        This method is only accessible within the execute() method.
        However, you may retrieve it manually by loading the begin data file using load_files() with the begin data nickname.

        Returns:
            MappingProxyType[str, Any]: An immutable mapping of the parameters that were passed to bind_inputs().

        Raises:
            ValueError: If called outside the execute() method.
        """
        if not self._is_executing:
            raise ValueError("get_bind_params() is only accessible during the execute() method. Retrieve using load_files() with begin data nickname if needed at other times.")
        return self._begin_params

    def purge_outputs(self, hyperactive: bool = False, avoid_running: bool = True) -> List[str]:
        """Purge pipe outputs from the working directory.

        Args:
            hyperactive: If True, removes all files except input files.
                        If False, only removes output files and end data.
            avoid_running: If True, prevents purging outputs while pipe is running.

        Returns:
            List[str]: The names of the files that were deleted.

        Raises:
            RuntimeError: If avoid_running is True and pipe is in RUNNING state

        Notes:
            In normal mode (hyperactive=False):
            - Always removes the end data file
            - Always removes the execute fails file
            - Removes all output files
            - Preserves input files
            - Preserves begin data file
            - Preserves other files

            In hyperactive mode (hyperactive=True):
            - Removes all files except begin data file
            - Preserves begin data file

            The begin data file is never removed as it contains essential
            configuration information about the pipe.
        """
        if avoid_running and self.get_state() == PipeState.RUNNING:
            raise RuntimeError(f"Cannot purge outputs while pipe is in {PipeState.RUNNING} state")

        # Always try to remove the execute fails file
        fails_file = self.resolve_nickname(SpecialPipeFileNickname.EXECUTE_FAILS)
        if fails_file.exists():
            fails_file.unlink()

        deleted_files = self._pti.purge_outputs(self.working_dir, hyperactive)
        self._clear_state_cache()  # Clear cache since we may have deleted end file
        return deleted_files

    def _save_begin_data(self, params: Optional[Dict[str, Any]] = None) -> None:
        """Saves pipe configuration and input validation data.
    
        This method creates the begin file which serves as:
        1. A marker that bind_inputs() has been called
        2. A record of inputs and outputs
        3. Storage for user-provided parameters
        4. Documentation of required private configs
    
        Args:
            params: Optional dictionary of parameters to store for the execute() phase.
                   These parameters will be accessible via get_bind_params() during execution.
        """
        # Convert inputs and outputs to string dictionaries
        inputs = {
            str(pattern): model_class.__name__ if model_class is not None else None
            for pattern, model_class in self._pti.inputs.values()
        }
        outputs = {
            str(pattern): model_class.__name__ if model_class is not None else None
            for pattern, model_class in self._pti.outputs.values()
        }

        # Create and save begin data
        begin_data = ToToPipeBeginData(
                                        task_classname=self.__class__.__name__,
                                        params=params or {},
                                        inputs=inputs,
                                        outputs=outputs,
                                        private_configs=self._pti.required_private_configs,
                                        heartbeat_timeout_secs=self._pti.heartbeat_timeout_secs
                                      )
        begin_data.save(str(SpecialPipeFileNickname.BEGIN.abspath(self.working_dir)))

    def _save_exception_data(self, exception: Exception) -> None:
        """Save exception data to the execute fails file.

        This method appends exception information to the execute fails file which serves as:
        1. A record of all exceptions that have occurred during execute()
        2. A debugging aid with full stack traces and timing information
        3. A history of failures if the pipe is retried multiple times

        Args:
            exception: The exception that was caught during execute()
        """
        # Try to load existing data, or create new if file doesn't exist
        fails_file = self.resolve_nickname(SpecialPipeFileNickname.EXECUTE_FAILS)
        try:
            if fails_file.exists():
                fails_data = ToToPipeExecuteFailsData.from_yaml(str(fails_file))
            else:
                fails_data = ToToPipeExecuteFailsData(exceptions=[])
        except Exception:
            # If we can't load the existing file, start fresh
            fails_data = ToToPipeExecuteFailsData(exceptions=[])

        # Create new exception info
        exc_info = ExceptionInfo(
            timestamp=datetime.now(),
            exception_type=exception.__class__.__name__,
            exception_message=str(exception),
            exception_args=[str(arg) for arg in exception.args],
            traceback=traceback.format_exc()
        )

        # Append to list and save
        fails_data.exceptions.append(exc_info)
        fails_data.save(str(fails_file))
        self._clear_state_cache()  # Clear cache since we created/modified fails file

    def get_failures(self) -> List[ExceptionInfo]:
        """Get the list of exceptions that occurred during pipe execution.

        Returns:
            List[ExceptionInfo]: A list of exception information objects, or an empty list if no failures occurred.
        """
        # If there is no failures file, return an empty list
        fails_file = self.resolve_nickname(SpecialPipeFileNickname.EXECUTE_FAILS)
        if not fails_file.exists():
            return []
            
        # Load and return the failures data
        fails_data = ToToPipeExecuteFailsData.load(str(fails_file))
        return fails_data.exceptions

    def get_state(self, cache_state_secs: float = 0.1) -> PipeState:
        """Get the current state of this pipe instance.

        This is an instance method that delegates to the PipeState.infer_state() method.
        It provides a convenient way to check the state of the current pipe instance.
        
        Args:
            cache_state_secs: Number of seconds to cache the state for. If 0, always recalculate.
                            Default is 0.1 seconds to optimize frequent calls within the same function.

        Returns:
            The current PipeState of this pipe instance
        """
        current_time = time.time()
        
        # If we have a cached state and it's fresh enough, return it
        if hasattr(self, '_cached_state') and hasattr(self, '_cached_state_time'):
            age = current_time - self._cached_state_time
            if cache_state_secs > 0 and age < cache_state_secs:
                return self._cached_state
        
        # Calculate new state and cache it
        state = PipeState.infer_state(Path(self.working_dir))
        self._cached_state = state
        self._cached_state_time = current_time
        return state

    def _clear_state_cache(self) -> None:
        """Clear the cached state to force recalculation on next get_state() call."""
        if hasattr(self, '_cached_state'):
            delattr(self, '_cached_state')
        if hasattr(self, '_cached_state_time'):
            delattr(self, '_cached_state_time')


    @staticmethod
    def _cache_private_configs(
        private_configs_path: Optional[Path],
        private_configs_src: Optional[Mapping[str, Any]],
        required_private_configs: List[str]
    ) -> Optional[MappingProxyType[str, Any]]:
        """Handle private config initialization and validation.

        Args:
            private_configs_path: Path to the private configs file, if any
            private_configs_src: Source mapping of private config values, if any
            required_private_configs: List of required private config keys

        Returns:
            MappingProxyType of private configs if private_configs_src is provided,
            None otherwise.

        Raises:
            ValueError: If required keys are missing from private_configs_src
        """
        if not (private_configs_src and private_configs_path):
            return None

        # Only check for missing keys if there are required keys
        if required_private_configs:
            missing_keys = set(required_private_configs) - set(private_configs_src.keys())
            if missing_keys:
                raise ValueError(f"Missing required private config keys: {missing_keys}")

        with AppConfigCachedSubset.open(private_configs_path) as cc:
            # Below code will save file only on change
            if cc.regen(private_configs_src, list(private_configs_src.keys())):
                cc.save()
        return None

    def cache_private_configs(self, private_configs_src: Mapping[str, Any]) -> None:
        """Cache private configuration values in the private configs file.
        
        This method allows caching private configuration values (like API keys, passwords)
        outside of the working directory. The values are stored in a separate file
        specified by the private_configs parameter during construction.
        
        Args:
            private_configs_src: Mapping containing the private config values to cache
            
        Raises:
            ValueError: If required private config keys are missing from private_configs_src
            ValueError: If no private_configs file path was specified during construction
        """
        self._cache_private_configs(
            self._private_configs_path(),
            private_configs_src,
            self.__class__.pipe_type_info().required_private_configs
        )

    def output(self):
        """Define the output target for this Luigi task.
        
        By default, this method returns a LocalTarget for the _pipe_completion.yaml file,
        which indicates successful completion of the task. This aligns with Luigi's
        standard completion tracking mechanism.
        
        Derived classes may override this method to specify additional files that
        must exist for the task to be considered complete. However, in most cases
        this is unnecessary since the _pipe_completion.yaml file is only created after
        all outputs have been successfully generated.
        
        Example of overriding in a derived class:
            def output(self):
                return [
                    luigi.LocalTarget(str(self.resolve_nickname("my_critical_output"))),
                    super().output()  # Include the completion file check
                ]
        
        Returns:
            luigi.LocalTarget: The _pipe_completion.yaml file target
        """
        import luigi
        return luigi.LocalTarget(str(self.resolve_nickname(SpecialPipeFileNickname.COMPLETION)))

    def clear_working_dir(self, remove_inputs: bool = False) -> None:
        """Clear the working directory of all files except inputs.
        
        Args:
            remove_inputs: If True, removes all files including inputs.
            If False, only removes output files and completion data.
        """
        # Get list of files to preserve
        preserve_files = set()
        if not remove_inputs:
            for pattern, _ in self._pti.inputs.values():
                preserve_files.update(pattern.matched_files(self.working_dir))
        
        # Delete all files except those in preserve_files
        for file in Path(self.working_dir).glob("*"):
            if file.is_file() and str(file) not in preserve_files:
                file.unlink()
        
        # Clear state cache since we modified files
        self._clear_state_cache()

    def clear_working_dir_except_begin(self) -> None:
        """Clear the working directory of all files except the begin file.
        
        This method:
        - Always preserves the begin file
        - Always removes the completion data file
        - Always removes the heartbeat file
        - Always removes all output files
        - Always removes all input files
        """
        # Get the begin file path
        begin_file = Path(self.working_dir) / SPECIAL_NICKNAMES[SpecialPipeFileNickname.BEGIN][0]
        
        # Delete all files except begin file
        for file in Path(self.working_dir).glob("*"):
            if file.is_file() and file != begin_file:
                file.unlink()
        
        # Clear state cache since we modified files
        self._clear_state_cache()

    @property
    def completion_data(self) -> ToToPipeCompletionData:
        """Access the completion data that will be written when the pipe completes.
        
        This property provides access to the completion data object during pipe execution.
        The object can be modified to store additional data that will be written to the
        completion file when the pipe finishes successfully.
        
        Returns:
            ToToPipeCompletionData: The completion data object that will be written
            
        Raises:
            RuntimeError: If accessed outside of execute() method
        """
        if not self._is_executing:
            raise RuntimeError(
                "completion_data can only be accessed during execute(). "
                "To read completion data outside of execute(), use load_files() with the completion nickname."
            )
        if self._completion_data is None:
            # Initialize with default values
            self._completion_data = ToToPipeCompletionData(
                timing=TimingInfo(
                    started_time=datetime.now(),
                    ended_time=datetime.now()
                ),
                outputs={},
                extra={}
            )
        return self._completion_data

    @classmethod
    def get_pipe_state(cls, working_dir: str | Path) -> PipeState:
        """Get the current state of a pipe from its working directory.

        Args:
            working_dir: The working directory of the pipe.

        Returns:
            The current state of the pipe.
        """
        pipe = cls.access_pipe(working_dir)
        return pipe.get_state()