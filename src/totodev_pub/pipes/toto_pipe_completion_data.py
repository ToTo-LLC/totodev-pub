# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Data model for pipe completion data.

This class is used to store information about a completed pipe execution.
"""

from datetime import datetime
from typing import Dict, List, Literal, Optional, NamedTuple, Any
from pydantic import BaseModel, Field, computed_field, model_validator, ConfigDict

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin


class AuxTimingEntry(NamedTuple):
    """A single auxiliary timing entry with a message and timestamp."""
    msg: str
    time: datetime


class FileVersion(NamedTuple):
    """Version information for a file format.
    
    Args:
        major: Major version number. Changes indicate breaking changes.
        minor: Minor version number. Changes should be backward compatible.
    """
    major: int
    minor: int

    def __str__(self) -> str:
        """Returns the version as a dotted string (e.g. '1.0')."""
        return f"{self.major}.{self.minor}"


class TimingInfo(BaseModel):
    """Timing information for a pipe execution."""
    started_time: datetime = Field(
        description="The time when the pipe execution started"
    )
    ended_time: datetime = Field(
        description="The time when the pipe execution completed"
    )
    aux_times: list[AuxTimingEntry] = Field(
        default_factory=list, 
        description="List of additional timing entries in chronological order"
    )

    def add_aux_timing(self, msg: str, log_time: Optional[datetime] = None) -> None:
        """Add an auxiliary timing entry.

        Entries may be added during ToToPipeBase.execute() by updating the my_pipe.completion_data.timing.aux_times list.
        
        Args:
            msg: A descriptive message for this timing entry
            log_time: Optional timestamp for this entry. If None, uses current time.
        """
        if log_time is None:
            log_time = datetime.now()
        self.aux_times.append(AuxTimingEntry(msg, log_time))

    model_config = ConfigDict(extra='allow')  # Allow extra fields in the input data


class ToToPipeCompletionData(BaseModel, FileMappedPydanticMixin):
    """Provides details about a completed run of ToToPipeBase derived class.
    
    Generally speaking, this structure should not be used to pass data out of your pipe. 
    Its best use is for logging and analysis data for the pipe creator, particularly using the fields
         ToToPipeCompletionData.timing.aux_times and ToToPipeCompletionData.extra.
    Many pipes will do absolutely nothing with this structure.

    To pass data out of your pipe, use the "outputs" of the pipe defined by it's pipe_type_info.

    This class is typically serialized to file as "_pipe_completion.yaml".
    """

    extra: Dict[str, Any] = Field( default_factory=dict, description="Additional data that should be set during execute() using the ToToPipeBase.completion_data.extra field")
    timing: TimingInfo = Field( default_factory=lambda: TimingInfo( started_time=datetime.now(), ended_time=datetime.now()), description="Timing information for the pipe execution")
    outputs: Dict[str, List[str]] = Field( description="For each output nickname, provide the files that match its pattern")
    this_file_version: FileVersion = Field( default=FileVersion(1, 0), description="Version information for this file format. Major version changes indicate breaking changes.")

    @property
    def duration_w_units(self) -> str:
        """Property that provides the duration with appropriate units.
        This field appears in serialized output but is ignored during deserialization.
        """
        return self._duration_w_units()

    def this_file_version_dotted(self) -> str:
        """Returns the file version as a dotted string.
        
        Returns:
            str: Version in format "major.minor" (e.g. "1.0")
        """
        return str(self.this_file_version)

    def _duration_w_units(self, time_units: Literal["seconds", "minutes", "hours", "days"] | None = None) -> str:
        """Get the duration as a string with appropriate units.
        
        If time_units is not specified, automatically selects the smallest unit where
        the duration would be >= 1.
        
        Args:
            time_units: Optional specific units to use. If None, automatically selects appropriate units.
        
        Returns:
            str: Duration with units, e.g. "1.5 hours" or "30.0 seconds"
        """
        if time_units is None:
            # Calculate in seconds first
            duration_secs = (self.timing.ended_time - self.timing.started_time).total_seconds()
            
            # Select appropriate units
            if duration_secs >= 86400:  # More than a day
                time_units = "days"
            elif duration_secs >= 3600:  # More than an hour
                time_units = "hours"
            elif duration_secs >= 60:  # More than a minute
                time_units = "minutes"
            else:
                time_units = "seconds"
        
        duration = (self.timing.ended_time - self.timing.started_time).total_seconds()
        if time_units == "minutes":
            duration /= 60
        elif time_units == "hours":
            duration /= 3600
        elif time_units == "days":
            duration /= 86400
            
        # Format with 2 decimal places and remove trailing zeros
        duration_str = f"{duration:.2f}".rstrip('0').rstrip('.')
        # Make unit singular if duration is 1
        unit = time_units[:-1] if duration == 1 else time_units
        
        return f"{duration_str} {unit}"

    model_config = ConfigDict(extra='allow')  # Allow extra fields in the input data

    @model_validator(mode='before')
    @classmethod
    def _convert_timing(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """Convert timing dict to TimingInfo object if needed."""
        if isinstance(data.get('timing', None), dict):
            data['timing'] = TimingInfo(**data['timing'])
        return data

    def absent_outputs(self) -> List[str]:
        """Returns a list of output nicknames that have no matching files.
        This method checks the outputs dictionary and returns a list of nicknames
        where either:
        - The nickname is missing from the outputs dictionary
        - The nickname exists but has an empty list of files
        
        Returns:
            List[str]: List of output nicknames that have no matching files.
            Empty list if all outputs have at least one matching file.
        """
        return [
            nickname for nickname, files in self.outputs.items()
            if not files  # Empty list means no files matched
        ]

