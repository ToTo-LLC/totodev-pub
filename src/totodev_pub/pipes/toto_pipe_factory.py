# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from typing import List, Type, Mapping, Callable, Optional, Any, Tuple, Dict, Union
from pathlib import Path
import fnmatch
import shutil
from totodev_pub.pipes.toto_pipe_base import ToToPipeBase
from totodev_pub.pipes.pipe_state import PipeState
from totodev_pub.minor.date_tree_folder import DateTreeFolder
from totodev_pub.pipes.app_config_cached_subset import AppConfigCachedSubset
from totodev_pub.pipes.toto_pipe_type_info import ToToPipeTypeInfo
from totodev_pub.pipes.toto_pipe_type_info import RelativeFilepathPattern
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.pipes.pipe_stat import PipeStat
from pydantic import BaseModel, Field, field_validator
from datetime import datetime, timedelta
import time
from typing import Generator
import re
import asyncio


DEFAULT_FACTORY_PIPE_RETAIN_DAYS = 7 # how long to retain working dirs befor purging
DEFAULT_FACTORY_PIPE_MONITOR_DAYS = 2 # how many days to monitor for status and completion
DEFAULT_CACHE_PIPE_STATS_SECS = 2 # how many seconds to cache pipe stats

class ToToPipeFactory:
    """Factory class for conveniently creating ToToPipeBase objects.
    
    This class is used to instantiate classes derived from ToToPipeBase.
    it provides a registry for such classes and convenience methods for initializing them:
    - creating a new working directory
    - moving/copying input files into the working directory
    - caching private configs
    - instantiating the pipe class
    - binding inputs

    Generally speaking, you would create instances of this class at startup of your program.
    Then, during the course of your program, you would call the make() method to create new pipe instances.
    
    It does not, however, spawn() the pipe.
    """

    PRIVATE_CONFIGS_FILENAME = "pipe_private_cachefile.yaml"


    def __init__(self,
                 pipe_tree_root: Path, 
                 config_obj: Mapping, 
                 retain_days:int=DEFAULT_FACTORY_PIPE_RETAIN_DAYS,
                 monitor_days:int=DEFAULT_FACTORY_PIPE_MONITOR_DAYS) -> None:
        """Initializes a "factory" for creating ToToPipeBase objects.

        Typicallly you only need one factory per process-lifetime, the same object can "make" any registered pipe type.
        Uses ToToPipeBase.registered_pipe_classes() to get the list of registered pipe classes.

        Note: This class also "cleans up" old working directories under its pipe_tree_root.
        
        Args:
            pipe_tree_root: The root of the pipe tree.
            config_obj: Typically the global CONFIG object from which private configs will be cached.
                        A reference to the config_obj is retained so that the private configs can be accessed by the factory.
            retain_days: Determines at what age working directories may be deleted.
            monitor_days: For functions which check for status and completion, this is the number of days to monitor (including today)
        IMPORTANT NOTE: The functioning of this object will create and delete files and directories within the pipe_tree_root.

        """
        self.pipe_tree_root:Path = pipe_tree_root
        self.config_obj:Mapping = config_obj # typically this is the global CONFIG object for the application
        self._folder_factories:dict[str, Callable[[Optional[str]], DateTreeFolder]] = {}
        self._retain_days:int = retain_days
        self._monitor_days:int = monitor_days
        self.pipe_tree_root.mkdir(parents=True, exist_ok=True) # this is a no-op if the directory already exists
        
        # Initialize cache attributes
        self._cached_pipe_stats: list[PipeStat] = []
        self._cached_pipe_stats_time: float = 0.0


    def _pclass(self,pipe_class_name:str) -> Type[ToToPipeBase]:
        """Returns the registered class object for a pipe class name """

        registered_classes = ToToPipeBase.registered_pipe_classes()
        if pipe_class_name not in registered_classes:
            raise ValueError(f"Pipe class {pipe_class_name} not found in ToToPipeBase.registered_pipe_classes()" \
                             "Registered classes currently include only: {registered_classes.keys()}"
                            )
        return registered_classes[pipe_class_name]
    
    def _pcategory(self,pipe_class_name:str) -> str:
        """Returns the category of the pipe class (which affects the directory tree)"""
        return pipe_class_name
        
    def _pcached_config_fpath(self,pipe_class_name:str) -> Path:
        """Returns the cached config file path for a pipe class."""
        # Store the config file one level up from the pipe_tree_root to avoid conflicts
        return self.pipe_tree_root.parent / self.PRIVATE_CONFIGS_FILENAME

    def _make_new_working_dir(self, pipe_class_name:str, uniq_src:Optional[str]=None) -> Path:
        """Creates a new folder for a pipe and returns the path to it.

        Args:
            pipe_class: The name of the pipe class to create (as appears in ToToPipeBase.registered_pipe_classes()).
            uniq_src: The unique source for the folder name created for the pipe.

        Implementation note: Date is already implicitly part of the path to the working dir, so including it in uniq_src is often unnecessary.
        If you pass none to uniq_src it'll use something like time of day to make a unique folder name.
        In a sense the uniq_src doesn't matter, but it can be helpful for devs doing debugging.
        To be clear, uniq_src doesn't have to be unique, but it is the seed of a unique folder name.
        """
        pcategory:str = self._pcategory(pipe_class_name)
        if pcategory not in self._folder_factories:
            self._folder_factories[pcategory] = DateTreeFolder.make_folder_factory(self.pipe_tree_root, pcategory, self._retain_days)
        new_working_dir:DateTreeFolder = self._folder_factories[pcategory](uniq_src)
        return new_working_dir.path
    

    def make(self,
             pipe_class_name: str,
             explicit_inputs: Dict[str, Union[str, FileMappedPydanticMixin, List[Union[str, FileMappedPydanticMixin]]]] = {},
             params: Optional[Dict[str, Any]] = None,
             uniq_src: Optional[str] = None
            ) -> ToToPipeBase:
        """Creates a new directory, instantiates the pipe, binds inputs, and returns the pipe instance.

        Does NOT spawn the pipe, For that you need to call the pipe instance's spawn() method.

        Args:
            pipe_class_name: The name of the pipe class to create as string, used in parent folder naming
            explicit_inputs: Dictionary mapping input nicknames to either:
                           - File paths as strings (files will be copied to working directory)
                           - Pydantic data objects (will be serialized to working directory)
                           - Lists of either of the above
            params: Optional dictionary of parameters to bind to the pipe instance
            uniq_src: Optional string to use as base for the working directory name (time of day used if not provided)

        Returns:
            ToToPipeBase: The initialized pipe instance ready for spawning

        Raises:
            ValueError: If a provided file path doesn't match any input pattern
            ValueError: If an input has an invalid type
            FileNotFoundError: If a provided file path doesn't exist
        """
        working_dir:Path = self._make_new_working_dir(pipe_class_name, uniq_src)
        pipe_class:Type[ToToPipeBase] = self._pclass(pipe_class_name)
        cached_config_fpath:Path = self._pcached_config_fpath(pipe_class_name)   
        # Ensure the private config path is absolute and outside the working directory
        private_config_path = str(cached_config_fpath.resolve())
        the_pipe:ToToPipeBase = pipe_class(working_dir=str(working_dir), private_configs=private_config_path)

        the_pipe.cache_private_configs(self.config_obj)

        pipe_type_info:ToToPipeTypeInfo = pipe_class.pipe_type_info()

        # Process file inputs and create necessary subdirectories
        file_inputs: Dict[str, List[Path]] = {}
        struct_inputs: Dict[str, Union[FileMappedPydanticMixin, List[FileMappedPydanticMixin]]] = {}

        # Segregate file paths and Pydantic objects
        for nickname, value in explicit_inputs.items():
            if isinstance(value, (str, Path)):
                file_inputs[nickname] = [Path(value)]
            elif isinstance(value, FileMappedPydanticMixin):
                struct_inputs[nickname] = value
            elif isinstance(value, list):
                if all(isinstance(v, (str, Path)) for v in value):
                    file_inputs[nickname] = [Path(v) for v in value]
                elif all(isinstance(v, FileMappedPydanticMixin) for v in value):
                    struct_inputs[nickname] = value
                else:
                    raise ValueError(f"All items in list for nickname '{nickname}' must be either all file paths or all Pydantic objects")
            else:
                raise ValueError(f"Invalid input type for nickname '{nickname}': {type(value)}")

        # Create subdirectories for input patterns
        patterns = [pattern for pattern, _ in pipe_type_info.inputs.values()]
        RelativeFilepathPattern.affirm_subdirs(working_dir, patterns)

        # Process and copy files
        for nickname, paths in file_inputs.items():
            if nickname not in pipe_type_info.inputs:
                raise ValueError(f"No input pattern found for nickname: {nickname}")
            
            pattern, _ = pipe_type_info.inputs[nickname]
            target_paths = []
            for src_path in paths:
                if not src_path.exists():
                    raise FileNotFoundError(f"Input file not found: {src_path}")
                
                # Get target path using the pattern
                target_path = pattern.calc_path(root_folder=working_dir)
                target_path = Path(working_dir) / target_path
                target_path.parent.mkdir(parents=True, exist_ok=True)
                
                # Copy the file if source and target are different paths
                if str(src_path.resolve()) != str(target_path.resolve()):
                    shutil.copy2(src_path, target_path)
                target_paths.append(str(target_path))
            
            # Add target paths to struct_inputs
            if len(target_paths) == 1:
                struct_inputs[nickname] = target_paths[0]
            else:
                struct_inputs[nickname] = target_paths

        # Bind inputs using the processed structs and params
        the_pipe.bind_inputs(
            explicit_inputs=struct_inputs,
            private_configs_src=self.config_obj,
            params=params
        )

        assert the_pipe.get_state() == PipeState.INITIALIZED
        return the_pipe


    async def list_pipe_stats(self,age_days:Optional[int]=None,cached_secs:Optional[float]=DEFAULT_CACHE_PIPE_STATS_SECS) -> list[PipeStat]:
        """Returns a list of all PipeStat objects by running pipe_stats() in another thread.
        
        This is a convenience method that converts the generator from pipe_stats() into a list.
        The operation is run in a separate thread to avoid blocking the event loop.
        
        Args:
            age_days: If provided, only pipes modified within the last age_days will be included.
            cached_secs: If provided, will only scan files and directories if our cached scan is older than this.
        """
        def collect_stats():
            return list(self.pipe_stats(age_days=age_days, cached_secs=cached_secs))
            
        return await asyncio.to_thread(collect_stats)

    def pipe_stats(self,age_days:Optional[int]=None,cached_secs:Optional[float]=DEFAULT_CACHE_PIPE_STATS_SECS) -> Generator[PipeStat,None,None]:
        """Returns a generator of PipeStat objects for all pipes in the pipe tree.
        
        Args:
            age_days: If provided, only pipes modified within the last age_days will be included.
            cached_secs: If provided, will only scan files and directories if our cached scan is older than this.
                        Set to 0 or None to always perform a fresh scan.
                        
        Returns:
            Generator yielding PipeStat objects for each pipe instance found.
            
        Note:
            The scan results are cached for cached_secs seconds to avoid excessive filesystem operations.
            Each new scan discards any previously cached results.
        """
        if self._monitor_days <= 0:
            raise ValueError("monitor_days must be greater than 0")

        current_time = time.time()
        
        # Check if we have a valid cached scan
        if cached_secs and current_time - self._cached_pipe_stats_time < cached_secs:
            yield from self._cached_pipe_stats
            return

        # Reset cache before starting new scan
        self._cached_pipe_stats = []
        self._cached_pipe_stats_time = 0.0

        # Calculate date range to scan
        end_date = datetime.now().date()
        begin_date = end_date - timedelta(days=self._monitor_days - 1)  # -1 because monitor_days includes today

        # Get all active dates in our monitoring window
        for date in DateTreeFolder.active_dates(begin_date, str(self.pipe_tree_root), end_date=end_date):
            # For each category (pipe class) on this date
            for category in DateTreeFolder.types_on_date(date, str(self.pipe_tree_root)):
                # For each instance of this category
                for instance in DateTreeFolder.instances_on_date(date, category, str(self.pipe_tree_root)):
                    working_dir = Path(self.pipe_tree_root) / date.strftime("%Y-%m") / f"{date.day:02d}-{date.strftime('%a')[:3]}" / category / instance
                    
                    # Skip if age_days filter is provided and directory is too old
                    if age_days is not None:
                        dir_mtime = datetime.fromtimestamp(working_dir.stat().st_mtime)
                        if (datetime.now() - dir_mtime).days > age_days:
                            continue
                    
                    # Create PipeStat object for this instance
                    pipe_stat = PipeStat.from_working_dir(working_dir)
                    self._cached_pipe_stats.append(pipe_stat)
                    yield pipe_stat

        # Update cache timestamp if caching is enabled
        if cached_secs:
            self._cached_pipe_stats_time = current_time

    def recently_completed(self, since_time: datetime) -> Generator[PipeStat,None,None]:
        """Returns a generator of PipeStat objects for all pipes that have completed since the given time.
        
        Only returns pipes that are in the COMPLETED state and were modified after since_time.
        
        Args:
            since_time: datetime object representing the cutoff time
        """
        for pipe_stat in self.pipe_stats():
            if pipe_stat.state.is_completed() and pipe_stat.last_modified > since_time:
                yield pipe_stat

