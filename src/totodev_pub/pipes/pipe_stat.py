# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from typing import List, Type, Any, Optional
from pathlib import Path
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
import yaml

from totodev_pub.pipes.toto_pipe_base import ToToPipeBase
from totodev_pub.pipes.pipe_state import PipeState
from totodev_pub.minor.date_tree_folder import DateTreeFolder
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.pipes.special_pipe_file_nickname import SpecialPipeFileNickname


class PipeStat(BaseModel, FileMappedPydanticMixin):
    """Represents the status information for a ToToPipeBase-derived pipe's working directory.

    You might get one of these from a ToToPipeFactory method call.
    Or, you can create one from a working directory path using PipeStat.from_working_dir().
    
    This data structure provides overview information obtained from scanning a working directory
    that contains the working files from a ToToPipeBase-derived class. It captures essential
    metadata about the pipe instance including its type, location, modification time, and current state.

    It further contains utility methods to let you get data and info about the pipe.
    Most of these utility methods are pretty-straight callthroughs to ToToPipeBase.

    NOTE: If you want to store information about these objects in a dictionary, the working dir can be thought of as a good (although long) primary key. 
    """
    working_dir: Path = Field(description="The path to the working directory containing the pipe's files and artifacts")
    pipe_class_name: str = Field(description="The class name of the ToToPipeBase-derived pipe implementation")
    last_modified: datetime = Field(description="The last modification timestamp of the working directory itself. " 
                                  "This typically reflects the newest modification time of any file within the directory")
    state: PipeState = Field(description="The current execution state of the pipe instance")

    @classmethod
    def from_working_dir(cls, working_dir: str | Path) -> "PipeStat":
        """Creates a PipeStat instance from a working directory.

        Args:
            working_dir: Path to the working directory.

        Returns:
            A PipeStat instance.

        Raises:
            ValueError: If working_dir doesn't exist
            ValueError: If _pipe_begin.yaml doesn't exist
        """
        working_dir = Path(working_dir)
        if not working_dir.exists():
            raise ValueError(f"Working directory must exist: {working_dir}")

        # Load begin data to get pipe class
        begin_file = working_dir / SpecialPipeFileNickname.BEGIN.filename()
        if not begin_file.exists():
            raise ValueError(f"Begin file must exist: {begin_file}")

        with open(begin_file, "r") as f:
            begin_data = yaml.safe_load(f)
            pipe_class_name = begin_data.get("pipe_class", "SampleWordStatsPipe")  # Default for backward compatibility

        # Get state and last modified time
        state = ToToPipeBase.get_pipe_state(str(working_dir))
        last_modified = datetime.fromtimestamp(working_dir.stat().st_mtime)

        return cls(
            working_dir=working_dir,
            pipe_class_name=pipe_class_name,
            last_modified=last_modified,
            state=state,
        )

    def get_pipe_class(self) -> Type[ToToPipeBase]:
        """Returns the actual pipe class type for this pipe instance.

        Requires that the pipe class be registered with ToToPipeBase.
        
        Returns:
            The ToToPipeBase-derived class type for this pipe.
            
        Raises:
            ValueError: If the pipe class is not found in the registered classes.
        """
        registered_classes = ToToPipeBase.registered_pipe_classes()
        if self.pipe_class_name not in registered_classes:
            raise ValueError(f"Pipe class {self.pipe_class_name} not found in registered classes. Available: {list(registered_classes.keys())}")
        return registered_classes[self.pipe_class_name]

    def get_pipe(self) -> ToToPipeBase:
        """Returns an actual pipe instance for this pipe instance.

        Requires that the pipe class be registered with ToToPipeBase.
        
        Returns:
            The actual pipe instance for this pipe.
            
        Raises:
            ValueError: If the pipe class is not found in the registered classes.
        """
        # raise an exception if the folder doesn't look initialized
        if not self.state.was_initialized():
            raise RuntimeError(f"The working_dir you provided may not have been correctly initialized: {self.working_dir}")
        pipe_class = self.get_pipe_class()
        return pipe_class(working_dir=str(self.working_dir))    

    def load_files(self, nickname: str) -> list[BaseModel]:
        """Very thin callthrough to ToToPipeBase.load_files().  """
        if not self.state.is_completed():
            raise RuntimeError(f"Cannot use this method to load files from pipe that is not completed. Current state: {self.state} for pipe at {self.working_dir}")
        
        objs:List[BaseModel] = self.get_pipe_class().pipe_type_info().files(self.working_dir, nickname, load_to_memory=True)
        return objs
    
    def list_files(self, nickname: str) -> list[str]:
        """Very thin callthrough to ToToPipeBase.list_files()."""
        if not self.state.is_completed():
            raise RuntimeError(f"Cannot use this method to load files from pipe that is not completed. Current state: {self.state} for pipe at {self.working_dir}")
        pipe = self.get_pipe()
        return pipe.list_files(nickname, abs_path=True)


    async def wait_for_completion(self, timeout_seconds: float | None = None) -> bool:
        """Thin passthrough to ToToPipeBase.wait_for_completion().

        May update the state and last_modified attributes of this instance.
        
        Args:
            timeout_seconds: Maximum time to wait in seconds. None means wait forever.
            
        Returns:
            True if the pipe completed successfully, False if it timed out or failed.
        """
        if self.state.is_completed():
            return True
        completion_data = await ToToPipeBase.wait_for_completion(self.working_dir, timeout_seconds)
        if completion_data is None:
            return False
        # Get fresh state from the working directory
        updated = self.from_working_dir(self.working_dir)
        # Update our instance attributes
        self.state = updated.state
        self.last_modified = updated.last_modified
        return True  # if we got past the guard clause above, it's completed

    ############################################################
    # Private properties and methods
    ############################################################

    _create_date_cached: Optional[datetime.date] = None  # Cache for create_date property

    @field_validator('working_dir')
    @classmethod
    def _ensure_absolute_path(cls, v: Path) -> Path:
        """Ensures the working_dir is an absolute path."""
        return v.resolve() if not v.is_absolute() else v

    @property
    def _determine_create_date(self) -> datetime.date:
        """Returns the date (without time) that this working directory was created.
        Uses the DateTreeFolder structure of the working_dir to determine the date.
        Result is cached after first calculation.
        """
        if self._create_date_cached is None:
            # Create a DateTreeFolder instance from the existing path
            dtf = DateTreeFolder(existing_folder_path=str(self.working_dir))
            self._create_date_cached = dtf.date
        return self._create_date_cached

    def __lt__(self, other: "PipeStat") -> bool:
        """Implements less than comparison for sorting.
        Sort priority: pipe_class_name, create_date (descending), last_modified (descending), working_dir
        """
        if not isinstance(other, PipeStat):
            return NotImplemented
        # First compare pipe_class_name
        if self.pipe_class_name != other.pipe_class_name:
            return self.pipe_class_name < other.pipe_class_name
        # Then compare create_date in descending order
        if self._determine_create_date != other._determine_create_date:
            return self._determine_create_date > other._determine_create_date  # Note: reversed for descending
        # Then compare last_modified in descending order
        if self.last_modified != other.last_modified:
            return self.last_modified > other.last_modified  # Note: reversed for descending
        # Finally compare working_dir as tie-breaker
        return str(self.working_dir) < str(other.working_dir)

    def __eq__(self, other: object) -> bool:
        """Implements equality comparison."""
        if not isinstance(other, PipeStat):
            return NotImplemented
        return (self.pipe_class_name == other.pipe_class_name and 
                self._determine_create_date == other._determine_create_date and 
                self.last_modified == other.last_modified and
                self.working_dir == other.working_dir)
