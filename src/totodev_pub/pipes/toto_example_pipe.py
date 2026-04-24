# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Demonstration of a basic ToToPipeBase implementation.

This module exists solely for educational purposes to show the fundamental patterns
of creating a Toto pipe. It implements a simple word statistics calculator to demonstrate:
1. Basic pipe structure and inheritance from ToToPipeBase
2. Input/output type definitions using ToToPipeTypeInfo
3. Parameter handling with Luigi
4. Simple execution flow

Note: This implementation prioritizes clarity over robustness and should not be used
as a template for production code.

Defining a pipe typically requires defining three at least three classes:
1. The pipe class itself (in our case SampleWordStatsPipe)
2. A Pydantic/FileMappedPydanticMixin model for the input data (in our case TextContent)
3. A Pydantic/FileMappedPydanticMixin model for the output data (in our case WordStats)

The pipe class defines the pipe's behavior, including:
- Input/output specifications
- Parameter handling
- Execution logic

The input and output models define the data structure and file mapping for the pipe's inputs and outputs.

It is also possible for the class to take/create raw files as input and output.
Note that this example does not show how to use private_configs.  See toto_pipe_factory.py for more about that.
"""

from typing import List, Dict, Any, Optional, Tuple, Literal, Union
from collections import Counter
from pydantic import BaseModel, Field
import os
import yaml
from pathlib import Path
import luigi
import re
import click
import tempfile
import shutil
import time
import asyncio
import sys

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.pipes.toto_pipe_base import ToToPipeBase, ToToPipeTypeInfo

# Supporting classes must be defined before the pipe class since they're referenced in _PTI
# ------------------------------------------------------------------------------

class TextContent(BaseModel, FileMappedPydanticMixin):
    """Simple container for text input.  Use for SampleWordStatsPipe. """
    text_content: str

class WordStats(BaseModel, FileMappedPydanticMixin):
    """Model for word statistics.  Use for SampleWordStatsPipe."""
    total_words: int = Field(default=0, description="Total number of words")
    avg_word_length: float = Field(default=0.0, description="Average word length")
    most_common: List[Tuple[str, int]] = Field(default_factory=list, description="Most common words and their counts")
    longest_words: List[str] = Field(default_factory=list, description="Longest words found")
    min_word_length_used: Optional[int] = Field(default=None, description="Minimum word length used for filtering")



class SampleWordStatsPipe(ToToPipeBase):
    """Demonstration pipe showing basic ToToPipeBase usage patterns.
    
    This example calculates simple word statistics to demonstrate:
    - How to define pipe inputs and outputs
    - Basic parameter handling through bind_inputs
    - Simple execution flow with state management
    
    This is for learning purposes only and intentionally simplified.
    """

    # Below is a great way to define the pipe type info is to have a class variable that is a ToToPipeTypeInfo instance
    _PTI = ToToPipeTypeInfo(
        inputs={"input_text": ("input/text.yaml", TextContent), 
                "dummy_pdfs": ("input/*.pdf", None)}, # useless but added for illustration
        outputs={"word_stats": ("output/stats.yaml", WordStats) },
        private_cfgs=[], # indicates no passwords or other private info needed.
        heartbeat_timeout_secs=10 # if heartbeat is older than 10 seconds, consider it stalled
    )

    @classmethod
    def pipe_type_info(cls) -> ToToPipeTypeInfo:
        """Return the pipe type info. Subclass of ToToPipeBase must implement."""
        return cls._PTI

    def bind_inputs(self, 
                    explicit_inputs: Dict[str, Union[str, FileMappedPydanticMixin, List[Union[str, FileMappedPydanticMixin]]]], 
                    private_configs_src: Optional[Dict[str, Any]] = None,
                    params: Optional[Dict[str, Any]] = None,
                    ignore_missing_inputs: bool = True
                   ) -> None:
        """Override the built-in behavior of ToToPipeBase.bind_inputs()
        
        Args:
            explicit_inputs: Dictionary mapping input nicknames to file paths or Pydantic objects
            private_configs_src: Optional dictionary containing private configuration data
            params: Optional dictionary of parameters
            ignore_missing_inputs: If True, missing required inputs will not raise an error
        """
        # Set default params if none provided
        if params is None:
            params = {}
        
        # Set default min_word_length if not provided
        if "min_word_length" not in params:
            params["min_word_length"] = 0

        # Validate required inputs before calling parent's bind_inputs
        if "input_text" not in explicit_inputs:
            raise ValueError("input_text file or object must be provided")

        # When ignore_missing_inputs is False, all inputs defined in _PTI must be provided
        if not ignore_missing_inputs:
            missing_inputs = set(self._PTI.inputs.keys()) - set(explicit_inputs.keys())
            if missing_inputs:
                raise ValueError(f"Missing required input: {', '.join(missing_inputs)}")

        # Validate min_word_length is valid
        if not isinstance(params['min_word_length'], int) or params['min_word_length'] < 0:
            raise ValueError("min_word_length must be a non-negative integer")

        # Call parent's bind_inputs with the provided ignore_missing_inputs value
        super().bind_inputs(explicit_inputs, 
                          private_configs_src=private_configs_src, 
                          params=params, 
                          ignore_missing_inputs=ignore_missing_inputs
                         )

    def execute(self) -> None:
        """Execute the word stats pipe.  Subclass of ToToPipeBase must implement."""

        # Load input text and values passed into bind_inputs
        text_content: TextContent = self.load_files("input_text")[0] # load into memory based on nickname
        min_word_length = self.get_bind_params()["min_word_length"]  # Required parameter, will raise KeyError if missing

        # Do the actual work
        stats = calculate_word_stats(text_content.text_content, min_word_length)
        ToToPipeBase._update_heartbeat(self.working_dir, new_txt="about to do hard stuff")
        time.sleep(0.3) # simulate some work
        ToToPipeBase._update_heartbeat(self.working_dir, new_txt="done with hard stuff")

        # Save the outputs
        output_file = self.resolve_nickname("word_stats")
        stats.save(str(output_file))

        # Base class will handle updating pipe status files and removing heartbeat file


    # def requires(self) -> List[luigi.Task]:
    #     """Return an empty list since this task has no dependencies.
    #     You may want to implement this if you have a dependency tree.
    #
    #     A clever strategy is to launch child pipes in subdirectories of
    #           `self.working_dir`
    #     """
    #     return [MyTask(self.working_dir / "my_eml_file.eml")]

    # def outputs(self) -> List[luigi.Target]:
    #     """Return an empty list since this task has no dependencies.
    #     ToToPipeBase provides very simple implementation of this method.
    #     """
    #     return []

ToToPipeBase.register_pipe_class(SampleWordStatsPipe) # Ensure base class can lookup this class.

# ------------------------------------------------------------------------------
# Supporting calculation functions below.  Not necessary to understnad pipes.
# 
# You might get some insight from the run_the_pipe() function below.
# However, you'd be better off understanding the toto_example_factory.py file
# which shows how to create a convenience function that binds inputs to a pipe
# and spawns it in various modes.
# ------------------------------------------------------------------------------

def calculate_word_stats(text: str, min_word_length: int = 0) -> WordStats:
    """Calculate word statistics from input text.
    
    Args:
        text: Input text to analyze
        min_word_length: Minimum length of words to include in stats
        
    Returns:
        WordStats object containing the calculated statistics
    """
    # Extract words and filter by minimum length if specified
    words = re.findall(r'\b\w+\b', text.lower())
    filtered_words = [word for word in words if len(word) >= min_word_length] if min_word_length > 0 else words

    # Calculate statistics
    total_words = len(filtered_words)
    avg_length = sum(len(word) for word in filtered_words) / total_words if total_words > 0 else 0.0
    word_counts = Counter(filtered_words)
    most_common = [(word, count) for word, count in word_counts.most_common(5)]  # Limit to top 5
    longest_words = sorted(set(filtered_words), key=len, reverse=True)[:10]

    return WordStats(
        total_words=total_words,
        avg_word_length=avg_length,
        most_common=most_common,
        longest_words=longest_words,
        min_word_length_used=min_word_length if min_word_length > 0 else None
    )

def run_the_pipe(text_content: TextContent, working_dir: Path, params: Optional[Dict[str, Any]] = None) -> WordStats:
    """Demonstrates the minimal code required to run a pipe with input and output.
    
    This function shows the essential steps to:
    1. Create and configure a pipe
    2. Send input data to it
    3. Execute the pipe
    4. Retrieve the results
    
    While the ToToPipeBase class offers additional capabilities for advanced scenarios,
    this pattern covers the most common use case.
    
    Args:
        text_content: The text content to analyze
        working_dir: Working directory for the pipe execution
        params: Optional parameters dictionary containing min_word_length
        
    Returns:
        WordStats object containing the analysis results
    """
    
    # Create, configure, and run pipe
    pipe = SampleWordStatsPipe(working_dir= str(working_dir))
    pipe.bind_inputs(explicit_inputs={"input_text": text_content}, params=params or {}, ignore_missing_inputs=True)
    pipe.spawn(mode="in-process")
    asyncio.run(ToToPipeBase.wait_for_completion(str(working_dir)))
    result: WordStats = pipe.load_files("word_stats")[0]
    return result



@click.command()
@click.option('--min-word-length', '-m', type=int, default=0, help='Minimum word length to include in statistics')
@click.option('--text', '-t', required=True, help='Text to analyze')
@click.option('--working-dir', '-w', type=click.Path(), help='Working directory for pipe execution. Will be created if it does not exist.')
def run_sample_pipe(min_word_length: int, text: str, working_dir: Optional[str]):
    """Run the sample word statistics pipe with text input.
    
    This is a demonstration of how to use the SampleWordStatsPipe class.
    It will create a working directory (if it doesn't exist), accept text input from the user,
    run the pipe, and display the results.
    """    
    # Set up working directory
    work_dir = Path(working_dir if working_dir else "sample-working-dir").resolve()
    work_dir.mkdir(parents=True) # should raise exception if it already exists
    
    # Create the input object to send into the pipe
    text_content = TextContent(text_content=text)
    stats = run_the_pipe( text_content=text_content, working_dir=work_dir, params={"min_word_length": min_word_length} if min_word_length > 0 else None)

    # Convert tuples to lists in most_common before dumping to YAML
    print(f"\n\nWord Stats:\n{stats.model_dump_json(indent=2)}")



if __name__ == '__main__':
    run_sample_pipe()

