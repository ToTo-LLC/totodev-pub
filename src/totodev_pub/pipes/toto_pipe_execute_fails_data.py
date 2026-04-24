# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Data structure for storing execution failures in ToToPipe instances.

This file contains the data structures used to record exceptions that occur during
the execution of a ToToPipe's execute() method. It captures detailed information
about each exception including stack traces and timing information.

The data is stored in _pipe_execute_fails.yaml in the pipe's working directory.
Multiple exceptions can be stored as the pipe may be retried multiple times.
"""

from datetime import datetime
from typing import List
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin

class ExceptionInfo(BaseModel):
    """Information about a single exception occurrence during pipe execution.
    
    Attributes:
        timestamp: When the exception occurred
        exception_type: The type of exception (e.g. "ValueError")
        exception_message: The str() of the exception
        exception_args: The exception's args tuple converted to strings
        traceback: The formatted traceback string
    """
    timestamp: datetime
    exception_type: str
    exception_message: str
    exception_args: List[str]
    traceback: str

class ToToPipeExecuteFailsData(BaseModel, FileMappedPydanticMixin):
    """Records all execution failures for a pipe.
    
    This class maintains a list of exceptions that have occurred during execution
    of a pipe's execute() method. Each time an exception occurs, a new ExceptionInfo
    is added to the list. The file persists between retries, building a history
    of failures that can be used for debugging and monitoring pipe reliability.
    
    The data is stored in _pipe_execute_fails.yaml in the pipe's working directory.
    
    Attributes:
        exceptions: List of exceptions in chronological order
    """
    exceptions: List[ExceptionInfo] 