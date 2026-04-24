# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Module containing the ToToPipeBeginData class for storing pipe initialization data."""

from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel, Field

from ..file_mapped_pydantic_mixin import FileMappedPydanticMixin

DEFAULT_HEARTBEAT_TIMEOUT_SECS = 60 # Used to infer if a process has hung/died

class ToToPipeBeginData(BaseModel, FileMappedPydanticMixin):
    """Data stored in the begin file for a pipe.
    
    This file serves as:
    1. A marker that bind_inputs() has been called
    2. A record of inputs and outputs
    3. Storage for user-provided parameters
    4. Documentation of required private configs
    """
    task_classname: str = Field(description="Name of the task class")
    params: Dict[str, Any] = Field(default_factory=dict, description="Parameters for the task")
    inputs: Dict[str, Optional[str]] = Field(default_factory=dict, description="Input file patterns and model classes. None indicates non-deserializable pattern.")
    outputs: Dict[str, Optional[str]] = Field(default_factory=dict, description="Output file patterns and model classes. None indicates non-deserializable pattern.")
    possible_outputs: Dict[str, Optional[str]] = Field(default_factory=dict, description="Possible output file patterns and model classes. None indicates non-deserializable pattern.")
    private_configs: List[str] = Field(default_factory=list, description="Required private configuration keys")
    created_time: datetime = Field(default_factory=datetime.now, description="When the begin file was created")
    heartbeat_timeout_secs: int = Field(default=60, description="Timeout in seconds for heartbeat file")

    def validate_inputs(self) -> bool:
        """Validate that all expected input files exist.
        
        Returns:
            bool: True if all inputs are valid, False otherwise
        """
        # This is a placeholder implementation since we don't have access to the actual file system
        # The actual implementation would check if all required input files exist
        return True

# Suggested additional methods:
# - validate_output_patterns(): Verify output file patterns are valid
# - get_input_file_path(nickname: str): Get full path for a given input nickname
# - get_expected_output_path(nickname: str): Get expected path for an output nickname
# - clone_with_updates(**kwargs): Create new instance with some fields updated
# - verify_task_class(): Verify the task_classname refers to a valid class 