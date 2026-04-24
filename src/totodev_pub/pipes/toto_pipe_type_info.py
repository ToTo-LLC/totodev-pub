# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Module containing the ToToPipeTypeInfo class for self-describing pipe characteristics.

This module provides a framework for describing the expected behaviors
of a pipe object (a.k.a. an object derived from ToToPipeBase) in terms of inputs, outputs, and performance characteristics.
"""

from typing import List, Dict, Type, Tuple, Any, Optional, Iterable
from pathlib import Path
import fnmatch

from .rel_fpath_pattern import RelativeFilepathPattern
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from pydantic import BaseModel

from totodev_pub.pipes.toto_pipe_begin_data import ToToPipeBeginData, DEFAULT_HEARTBEAT_TIMEOUT_SECS
from totodev_pub.pipes.toto_pipe_completion_data import ToToPipeCompletionData
from totodev_pub.pipes.toto_pipe_execute_fails_data import ToToPipeExecuteFailsData
from totodev_pub.pipes.special_pipe_file_nickname import SpecialPipeFileNickname

# Special nicknames dictionary mapping nickname to (glob, model) tuples
SPECIAL_NICKNAMES = {
    SpecialPipeFileNickname.BEGIN: ("_pipe_begin.yaml", ToToPipeBeginData),
    SpecialPipeFileNickname.COMPLETION: ("_pipe_completion.yaml", ToToPipeCompletionData),
    SpecialPipeFileNickname.HEARTBEAT: ("_pipe_heartbeat.txt", None),
    SpecialPipeFileNickname.EXECUTE_FAILS: ("_pipe_execute_fails.yaml", ToToPipeExecuteFailsData)
}

class ToToPipeTypeInfo:
    """Self-description of class derived from ToToPipeBase.
    
    This class provides a structured way to describe and document the expected behavior,
    inputs, outputs, and performance characteristics of a pipe object.

    Note that this is NOT bound to a specific pipe instance or directory.
    Although, it may be used to find files within a working directory.
    """

    # Special file nicknames and patterns
    PIPE_BEGIN_NICKNAME = SpecialPipeFileNickname.BEGIN
    PIPE_COMPLETION_NICKNAME = SpecialPipeFileNickname.COMPLETION
    PIPE_HEARTBEAT_NICKNAME = SpecialPipeFileNickname.HEARTBEAT

    def __init__(
        self,
        inputs: Dict[str, Tuple[str | RelativeFilepathPattern, Optional[Type[FileMappedPydanticMixin]]]] = None,
        outputs: Dict[str, Tuple[str | RelativeFilepathPattern, Optional[Type[FileMappedPydanticMixin]]]] = None,
        private_cfgs: List[str] = None,
        heartbeat_timeout_secs: int = 60
    ):
        """Initialize a new ToToPipeTypeInfo instance.

        Args:
            inputs: Dictionary mapping nicknames to (pattern, model_class) tuples.
                   Pattern can be a string or RelativeFilepathPattern.
                   Model class must be a subclass of FileMappedPydanticMixin or None.
            outputs: Dictionary mapping nicknames to (pattern, model_class) tuples.
                   Pattern can be a string or RelativeFilepathPattern.
                   Model class must be a subclass of FileMappedPydanticMixin or None.
            private_cfgs: List of required private configuration keys.
            heartbeat_timeout_secs: Number of seconds to wait for heartbeat before
                                  considering the pipe dead.

        Raises:
            TypeError: If inputs or outputs contain invalid types
            ValueError: If there are nickname collisions between inputs and outputs
        """
        # Initialize empty dictionaries if None
        inputs = inputs or {}
        outputs = outputs or {}
        private_cfgs = private_cfgs or []

        # Validate input types
        for nickname, (pattern_str, model_class) in inputs.items():
            if not isinstance(pattern_str, (str, RelativeFilepathPattern)):
                raise TypeError(f"Input glob pattern for '{nickname}' must be a string")
            if model_class is not None and not issubclass(model_class, FileMappedPydanticMixin):
                raise TypeError(f"Input model type for '{nickname}' must be None or a subclass of FileMappedPydanticMixin")

        # Validate output types
        for nickname, (pattern_str, model_class) in outputs.items():
            if not isinstance(pattern_str, (str, RelativeFilepathPattern)):
                raise TypeError(f"Output glob pattern for '{nickname}' must be a string")
            if model_class is not None and not issubclass(model_class, FileMappedPydanticMixin):
                raise TypeError(f"Output model type for '{nickname}' must be None or a subclass of FileMappedPydanticMixin")

        # Validate private_cfgs type
        if not isinstance(private_cfgs, list):
            raise TypeError("private_cfgs must be a list of strings")
        for cfg in private_cfgs:
            if not isinstance(cfg, str):
                raise TypeError("private_cfgs must be a list of strings")

        # Convert inputs and outputs to RelativeFilepathPattern objects
        self._inputs = {
            nickname: (RelativeFilepathPattern(pattern_str), model_class)
            for nickname, (pattern_str, model_class) in inputs.items()
        }

        # Convert outputs to RelativeFilepathPattern objects
        self._outputs = {
            nickname: (RelativeFilepathPattern(pattern_str), model_class)
            for nickname, (pattern_str, model_class) in outputs.items()
        }

        # Check for nickname collisions
        input_nicknames = set(self._inputs.keys())
        output_nicknames = set(self._outputs.keys())
        if input_nicknames & output_nicknames:
            raise ValueError("Nicknames cannot appear in both inputs and outputs")

        # Store private configs
        self._private_cfgs = private_cfgs
        self._heartbeat_timeout_secs = heartbeat_timeout_secs

    @property
    def required_private_configs(self) -> List[str]:
        """Get the list of required private configuration keys."""
        return self._private_cfgs

    @classmethod
    def create_from_pipe_begin(
        cls,
        working_dir: Optional[str | Path] = None,
        begin_obj: Optional[ToToPipeBeginData] = None
    ) -> 'ToToPipeTypeInfo':
        """Create a ToToPipeTypeInfo instance from a begin file or object.
        
        Args:
            working_dir: Path to the working directory containing _pipe_begin.yaml
            begin_obj: Optional ToToPipeBeginData object to use directly
            
        Returns:
            New ToToPipeTypeInfo instance
            
        Raises:
            ValueError: If neither working_dir nor begin_obj is provided
            ValueError: If working_dir is provided but _pipe_begin.yaml doesn't exist
        """
        if begin_obj is None:
            if working_dir is None:
                raise ValueError("Must provide either working_dir or begin_obj")
            working_dir = Path(working_dir)
            begin_file = working_dir / "_pipe_begin.yaml"
            if not begin_file.exists():
                raise ValueError(f"Begin file not found: {begin_file}")
            begin_obj = ToToPipeBeginData.load(begin_file)

        # Convert inputs and outputs to the format expected by __init__
        inputs = {
            nickname: (pattern, None) for nickname, pattern in begin_obj.inputs.items()
        }
        outputs = {
            nickname: (pattern, None) for nickname, pattern in begin_obj.outputs.items()
        }

        return cls(
            inputs=inputs,
            outputs=outputs,
            private_cfgs=begin_obj.private_configs,
            heartbeat_timeout_secs=begin_obj.heartbeat_timeout_secs
        )

    @property
    def inputs(self) -> Dict[str,Tuple[RelativeFilepathPattern, None|Type[FileMappedPydanticMixin]]]:
        """Get the dictionary of input patterns and their associated model classes.
        
        Returns:
            Dictionary mapping nicknames to (pattern, model) tuples
        """
        return self._inputs
    
    @property
    def outputs(self) -> Dict[str,Tuple[RelativeFilepathPattern, None|Type[FileMappedPydanticMixin]]]:
        """Maps a nickname to a tuple of a RelativeFilepathPattern and a Pydantic model.
        
        Represents the file pattern where output files might be stored,
        and a way to easily (de)serialize the file into a Pydantic model.
        If the pydantic model is None, (de)serializing the file is not simple.
        """
        return self._outputs
    
    @property
    def heartbeat_timeout_secs(self) -> Optional[int]:
        """Timeout in seconds for heartbeat checks, used to infer if a process has hung/died."""
        return self._heartbeat_timeout_secs

    def _get_pattern_lookup(self, nickname: str) -> tuple[RelativeFilepathPattern, Type[BaseModel] | None]:
        """Get the pattern and model class for a given nickname.
        
        Args:
            nickname: The nickname to look up
            
        Returns:
            Tuple of (pattern, model_class) for the given nickname
            
        Raises:
            ValueError: If nickname not found in inputs, outputs, or special nicknames
        """
        # Check special nicknames first
        if nickname in SPECIAL_NICKNAMES:
            pattern_str, model_class = SPECIAL_NICKNAMES[nickname]
            return (RelativeFilepathPattern(pattern_str), model_class)
            
        # Check inputs
        if nickname in self._inputs:
            return self._inputs[nickname]
            
        # Check outputs
        if nickname in self._outputs:
            return self._outputs[nickname]
            
        raise ValueError(f"Nickname '{nickname}' not found in inputs, outputs, or special nicknames")

    def _get_special_pattern(self, nickname: str) -> Optional[tuple[RelativeFilepathPattern, Type[BaseModel] | None]]:
        """Get the pattern and model class for a special nickname.
        
        Args:
            nickname: The nickname to look up
            
        Returns:
            Tuple of (pattern, model_class) if found, None otherwise
        """
        if nickname in SPECIAL_NICKNAMES:
            pattern_str, model_class = SPECIAL_NICKNAMES[nickname]
            return (RelativeFilepathPattern(pattern_str), model_class)
        return None

    def files(self, working_dir: str | Path, nickname: str, load_to_memory: bool = False, abs_path: bool = True) -> list[Path] | list[BaseModel]:
        """Find and return a list of existing file paths or deserialized objects for the given nickname.

        Args:
            working_dir: The working directory to search in
            nickname: The nickname to find files for
            load_to_memory: If True, load the files into memory as Pydantic models
            abs_path: If True, return absolute paths, otherwise return relative paths

        Returns:
            List of Path objects if load_to_memory is False, list of BaseModel objects if True
            Returns empty list if no files found or if nickname doesn't exist

        Raises:
            ValueError: If nickname not found in inputs or outputs
            ValueError: If load_to_memory is True but model class is None
            ValueError: If load_to_memory is True but model class doesn't inherit from FileMappedPydanticMixin
        """
        working_dir = Path(working_dir)
        pattern, model_class = self._get_pattern_lookup(nickname)
        matched_files = pattern.matched_files(working_dir)
        
        if not matched_files:
            return []
        
        if load_to_memory:
            if model_class is None:
                raise ValueError(f"Cannot load files for nickname '{nickname}' as it has no model class")
            if not issubclass(model_class, FileMappedPydanticMixin):
                raise ValueError(f"Model class for nickname '{nickname}' must inherit from FileMappedPydanticMixin")
            return [model_class.load(str(f)) for f in matched_files]
        
        # Convert paths to relative if needed
        if not abs_path:
            return [f.absolute() for f in matched_files]
        return matched_files

    def create_pattern_subdirs(self, working_dir: str | Path) -> None:
        """Create subdirectories for all patterns in the working directory.
        
        This method ensures that all subdirectories implied by the patterns in inputs
        and possible_outputs exist in the working directory.
        
        For example, if a pattern is "subdir/file_*.txt", this method will create
        the "subdir" directory if it doesn't exist.
        
        Args:
            working_dir: Path to the working directory where subdirs should be created
        """
        working_dir = Path(working_dir)
        
        # Create subdirs for special patterns
        for pattern_str, _ in SPECIAL_NICKNAMES.values():
            pattern = RelativeFilepathPattern(pattern_str)
            pattern.create_subdir(working_dir)
            
        # Create subdirs for inputs and outputs
        for pattern, _ in self.inputs.values():
            pattern.create_subdir(working_dir)
            
        for pattern, _ in self.outputs.values():
            pattern.create_subdir(working_dir)

    def missing_inputs(self, working_dir: str | Path) -> Dict[str, str]:
        """Check which input files are missing from the working directory.
        
        Args:
            working_dir: Path to the working directory to check
            
        Returns:
            Dictionary mapping nicknames to patterns for missing input files
        """
        missing = {}
        for nickname, (pattern, _) in self.inputs.items():
            if not pattern.matched_files(working_dir):
                missing[nickname] = str(pattern)
        return missing

    def persist_input(self, working_dir: str | Path, nickname: str, input_value: str | FileMappedPydanticMixin, merge_str: str | None = None) -> Path:
        """Write an input value to the working directory.
        
        Args:
            working_dir: Path to the working directory where the file should be written
            nickname: The nickname of the pattern to use
            input_value: Either a file path to copy or a pydantic object to serialize
            merge_str: Optional string to replace wildcards in the pattern
            
        Returns:
            Path to the written file
            
        Raises:
            ValueError: If nickname is not found in inputs
            ValueError: If input_value is a pydantic object but model class is None
            ValueError: If input_value is a pydantic object but doesn't match model class
            FileNotFoundError: If input_value is a file path that doesn't exist
        """
        if not (pattern_info := self.inputs.get(nickname)):
            raise ValueError(f"Nickname '{nickname}' not found in inputs")
        pattern, model_class = pattern_info

        # Check wildcard requirements
        wildcard_count = pattern.wildcard_count()
        if wildcard_count > 0 and merge_str is None:
            raise ValueError(
                f"Pattern '{pattern}' contains wildcards but no merge_str was provided. "
                "Please provide a merge_str to specify the target filename."
            )
        elif wildcard_count == 0 and merge_str is not None:
            raise ValueError(
                f"merge_str '{merge_str}' was provided but pattern '{pattern}' has no wildcards. "
                "merge_str should only be used with wildcard patterns."
            )

        # Calculate the target path
        target_path = pattern.calc_path(root_folder=working_dir, merge=[merge_str] if merge_str else [])

        # Create parent directories if they don't exist
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Handle file path vs data object
        if isinstance(input_value, str):
            # Copy file preserving metadata
            source_path = Path(input_value).resolve()
            target_path = target_path.resolve()
            
            if source_path == target_path:
                # If source and target are the same file, no need to copy
                return target_path
                
            if not source_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_value}")
            import shutil
            shutil.copy2(source_path, target_path)
        else:
            # Serialize data object
            if model_class is None:
                raise ValueError(
                    f"Cannot persist data object for nickname '{nickname}' as it is marked "
                    "as non-deserializable (model class is None)"
                )
            input_value.save(file_path=str(target_path))

        return target_path

    def purge_outputs(self, working_dir: str | Path, hyperactive: bool = False) -> List[str]:
        """Remove output files from the working directory.
        
        Args:
            working_dir: Path to the working directory
            hyperactive: If True, remove all files except inputs
            
        Returns:
            List of deleted file paths
            
        Notes:
        - Always removes the completion marker file ({PIPE_COMPLETION_NICKNAME})
        - If hyperactive=True, removes all files except inputs
        - If hyperactive=False, only removes outputs and completion marker
        """
        working_dir = Path(working_dir)
        if not working_dir.exists():
            raise ValueError(f"Working directory must exist: {working_dir}")

        deleted_files: List[str] = []

        # Always try to remove end file
        end_pattern = RelativeFilepathPattern(SPECIAL_NICKNAMES[self.PIPE_COMPLETION_NICKNAME][0])
        for file_path in end_pattern.matched_files(working_dir):
            Path(file_path).unlink()
            deleted_files.append(str(Path(file_path).relative_to(working_dir)))

        if hyperactive:
            # Get list of expected input patterns
            input_patterns = [pattern for pattern, _ in self.inputs.values()]
            # Get the begin file pattern
            begin_pattern = RelativeFilepathPattern(SPECIAL_NICKNAMES[self.PIPE_BEGIN_NICKNAME][0])
            
            # Walk through all files in working_dir
            for file_path in working_dir.rglob("*"):
                if file_path.is_file():
                    rel_path = file_path.relative_to(working_dir)
                    # Check if file matches any expected input pattern or is the begin file
                    is_expected = any(file_path in pattern.matched_files(working_dir) for pattern in input_patterns)
                    is_begin_file = file_path in begin_pattern.matched_files(working_dir)
                    if not (is_expected or is_begin_file):
                        file_path.unlink()
                        deleted_files.append(str(rel_path))
        else:
            # Only remove files matching possible_outputs patterns
            for pattern, _ in self.outputs.values():
                for file_path in pattern.matched_files(working_dir):
                    Path(file_path).unlink()
                    deleted_files.append(str(Path(file_path).relative_to(working_dir)))

        return deleted_files

    def calc_filepath(self, working_dir: str | Path, nickname: str, merge_str: str | None = None) -> Path:
        """Calculate the filepath for a given nickname and optional merge string.
        
        Args:
            working_dir: The working directory to use as the root
            nickname: The nickname to calculate the filepath for
            merge_str: Optional string to merge into wildcard patterns
            
        Returns:
            The calculated filepath
            
        Raises:
            ValueError: If the working directory doesn't exist
            ValueError: If the nickname is not found in inputs or outputs
            ValueError: If merge_str is provided for a pattern without wildcards
            ValueError: If the pattern has wildcards but no merge_str is provided
        """
        working_dir = Path(working_dir)
        if not working_dir.exists():
            raise ValueError(f"Working directory must exist: {working_dir}")

        pattern_info = (
            self.inputs.get(nickname) or
            self.outputs.get(nickname) or
            (nickname in SPECIAL_NICKNAMES and
             (RelativeFilepathPattern(SPECIAL_NICKNAMES[nickname][0], root_folder=working_dir), SPECIAL_NICKNAMES[nickname][1]))
        )
    
        if not pattern_info:
            raise ValueError(f"Nickname '{nickname}' not found in inputs or outputs")
    
        pattern, model_class = pattern_info
        
        # Check if merge_str is provided for a non-wildcard pattern
        if merge_str is not None and not pattern.has_wildcards:
            raise ValueError(f"merge_str '{merge_str}' was provided but pattern '{pattern}' has no wildcards")
        
        # Check if merge_str is missing for a wildcard pattern
        if pattern.has_wildcards and merge_str is None:
            raise ValueError(f"Pattern '{pattern}' has wildcards but merge_str is not provided")
    
        # Let RelativeFilepathPattern handle the path calculation with the working directory
        return pattern.calc_path(merge=[merge_str] if merge_str is not None else [], root_folder=working_dir)

    @classmethod
    def get_special_file_path(cls, working_dir: str | Path, special_nickname: str) -> Path:
        """Get the path to a special file (begin, end, or heartbeat) in the working directory.
        
        Args:
            working_dir: The working directory containing the special file
            special_nickname: The nickname of the special file (e.g. PIPE_BEGIN_NICKNAME)
            
        Returns:
            Path to the special file
            
        Raises:
            ValueError: If the special_nickname is not recognized
            ValueError: If working_dir doesn't exist
        """
        if special_nickname not in SPECIAL_NICKNAMES:
            raise ValueError(f"Unknown special nickname: {special_nickname}")
            
        working_dir = Path(working_dir)
        if not working_dir.exists():
            raise ValueError(f"Working directory must exist: {working_dir}")
            
        return working_dir / SPECIAL_NICKNAMES[special_nickname][0]

    @staticmethod
    def suggest_input_filenames_from_patterns(
        source_files: List[Path],
        working_dir: Path,
        input_patterns: Optional[Iterable[RelativeFilepathPattern]] = None,
    ) -> List[Path]:
        """Suggest target filenames for source files based on input patterns.

        This utility method takes a list of source files and determines appropriate target filenames
        within the working directory structure based on the input patterns. It uses glob pattern 
        matching to determine which pattern each source file matches. If multiple source files would 
        map to the same target path, they are automatically renamed with a zero-padded number 
        (e.g. file001.jpg, file002.jpg).

        Args:
            source_files: List of source file paths to match against patterns
            working_dir: The target working directory where files would be placed
            input_patterns: Optional iterable of RelativeFilepathPattern objects to match against.

        Returns:
            List of Path objects representing suggested target locations for each source file.
            The list has the same length as source_files, with each entry being the recommended
            target path for the corresponding source file. If a source file doesn't match any pattern,
            its target path will be None.

        Example:
            >>> patterns = [RelativeFilepathPattern("data/*.txt")]
            >>> source_files = [Path("input.txt"), Path("input.txt")]
            >>> result = ToToPipeTypeInfo.suggest_input_filenames(source_files, working_dir, patterns)
            >>> # result might be [working_dir/"data"/"input001.txt", working_dir/"data"/"input002.txt"]
        """
        if not isinstance(source_files, list):
            raise TypeError("source_files must be a list")
        if not isinstance(working_dir, Path):
            raise TypeError("working_dir must be a Path")
        if input_patterns is not None and not hasattr(input_patterns, '__iter__'):
            raise TypeError("input_patterns must be an iterable")
            
        target_paths: List[Optional[Path]] = []
        corresponding_patterns = []
        
        # Convert patterns to list and sort by specificity
        pattern_list = []
        if input_patterns:
            for pattern in input_patterns:
                if not isinstance(pattern, RelativeFilepathPattern):
                    raise TypeError(f"All input patterns must be RelativeFilepathPattern objects, got {type(pattern)}")
                pattern_path = Path(pattern.pattern)
                # Get just the filename pattern part and directory depth
                pattern_list.append((pattern, pattern_path.name, len(pattern_path.parts) - 1))
                
        # Sort patterns to match most specific first:
        # 1. Patterns with directories before those without
        # 2. Patterns with fewer wildcards before those with more
        # 3. More specific filenames before less specific ones
        pattern_list.sort(key=lambda x: (
            -x[2],  # Negative directory depth (more dirs = higher priority)
            0 if x[0].wildcard_count() == 0 else 1,  # Non-wildcard patterns first
            0 if x[1] != '*' else 1,  # Non-* patterns before * patterns
            -x[0].wildcard_count()  # Fewer wildcards = higher priority
        ))
        
        # First pass - get initial target paths
        for source_file in source_files:
            if not isinstance(source_file, Path):
                raise TypeError("All source_files must be Path objects")
                
            target_path = None
            matched_pattern = None  # Track pattern even when no match
            for pattern, pattern_name, _ in pattern_list:
                if fnmatch.fnmatch(source_file.name, pattern_name):
                    # Get the directory part of the pattern
                    pattern_path = Path(pattern.pattern)
                    target_dir = working_dir / pattern_path.parent
                    target_path = target_dir / source_file.name
                    matched_pattern = pattern_name
                    break  # Stop after first match
            target_paths.append(target_path)
            corresponding_patterns.append(matched_pattern)  # Will be None if no match

        # Second pass - handle duplicates
        path_counts: Dict[Path, int] = {}
        final_paths: List[Optional[Path]] = []

        for target_path in target_paths:
            if target_path is None:
                final_paths.append(None)
                continue

            if target_path in path_counts:
                # Path exists, increment counter and create numbered version
                path_counts[target_path] += 1
                count = path_counts[target_path]
                stem = target_path.stem
                suffix = target_path.suffix
                new_name = f"{stem}{count:03d}{suffix}"
                final_paths.append(target_path.parent / new_name)
            else:
                # First occurrence of this path
                path_counts[target_path] = 1
                if list(target_paths).count(target_path) > 1:
                    # Will have duplicates later, so number the first one
                    stem = target_path.stem
                    suffix = target_path.suffix
                    new_name = f"{stem}001{suffix}"
                    final_paths.append(target_path.parent / new_name)
                else:
                    # No duplicates expected
                    final_paths.append(target_path)

        # assert that the final_paths satisfy the corresponding_patterns
        assert len(final_paths) == len(corresponding_patterns) == len(source_files), "Mismatched lengths in results"
        for source_file, final_path, pattern_name in zip(source_files, final_paths, corresponding_patterns):
            if final_path is not None:
                assert pattern_name is not None, f"Logic error: have final_path [{final_path}] but no pattern for source [{source_file}]"
                assert fnmatch.fnmatch(final_path.name, pattern_name), f"Logic error: target path [{final_path}] does not match pattern [{pattern_name}] for source [{source_file}]"
            else:
                assert pattern_name is None, f"Logic error: have pattern [{pattern_name}] but no final_path for source [{source_file}]"
                    
        return final_paths

    def suggest_input_filenames(
        self,
        source_files: List[Path],
        working_dir: Path,
    ) -> List[Path]:
        """Suggest target filenames for source files based on this instance's input patterns.

        This is a convenience wrapper around suggest_input_filenames_from_patterns that uses
        the input patterns defined in this instance.

        Args:
            source_files: List of source file paths to match against patterns
            working_dir: The target working directory where files would be placed

        Returns:
            List of Path objects representing suggested target locations for each source file.
            The list has the same length as source_files, with each entry being the recommended
            target path for the corresponding source file. If a source file doesn't match any pattern,
            its target path will be None.

        Example:
            >>> pipe_type_info = ToToPipeTypeInfo(...)  # with input patterns defined
            >>> source_files = [Path("input.txt"), Path("data.csv")]
            >>> result = pipe_type_info.suggest_input_filenames(source_files, working_dir)
        """
        # Extract patterns from inputs dictionary
        input_patterns = [
            pattern if isinstance(pattern, RelativeFilepathPattern) else RelativeFilepathPattern(pattern)
            for pattern, _ in self.inputs.values()
        ]
        
        return self.suggest_input_filenames_from_patterns(source_files, working_dir, input_patterns)
