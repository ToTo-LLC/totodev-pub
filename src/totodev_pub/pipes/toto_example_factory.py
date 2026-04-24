# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Example demonstrating how to use the ToToPipeFactory class.

This module serves as a practical demonstration/tutorial of ToToPipeFactory usage patterns.
It is NOT part of the core functionality, but rather shows developers how to:

1. Set up and configure a ToToPipeFactory instance
2. Create convenience functions for spawning specific pipe types
3. Monitor pipe execution and query status
4. Structure a CLI interface for pipe management

The example uses SampleWordStatsPipe as a demonstration pipe type - the actual
word statistics functionality is not the focus. The key takeaways are the factory
usage patterns which can be applied to any ToToPipeBase-derived pipe in a real
implementation.

Example Usage:
    # Create a pipe to analyze some text
    pipe = spawn_text_stats_pipe("sample text")
    
    # Monitor execution
    factory = THE_PIPE_FACTORY
    for stat in factory.pipe_stats():
        print(f"Pipe {stat.pipe_class} is in state {stat.state}")
"""

from pathlib import Path
import click
from typing import Literal, Optional
import asyncio
from datetime import datetime, timedelta
import tempfile
import atexit
import shutil

from totodev_pub.pipes.toto_pipe_factory import ToToPipeFactory
from totodev_pub.pipes.toto_example_pipe import SampleWordStatsPipe, TextContent
from totodev_pub.pipes.pipe_state import PipeState

CONFIG = {"SOME_API_KEY": "1234567890"} # dummy config object

# Create a temporary directory that will be cleaned up when the program exits
TEMP_ROOT = tempfile.mkdtemp(prefix="toto_pipe_factory_")
PIPE_FACTORY_ROOT_DIR = Path(TEMP_ROOT)

# Register cleanup function
def cleanup_temp_dir():
    """Clean up the temporary directory when the program exits."""
    if PIPE_FACTORY_ROOT_DIR.exists():
        shutil.rmtree(PIPE_FACTORY_ROOT_DIR)

atexit.register(cleanup_temp_dir)

########################################################
# Code below this line is the key part of the example.  
#
# These are the bits you'd want to build on for your own pipe factories.
########################################################

# Typically you would create a global factory object in your config_apply.py file
THE_PIPE_FACTORY:ToToPipeFactory = ToToPipeFactory(
                                                    pipe_tree_root=PIPE_FACTORY_ROOT_DIR,
                                                    config_obj=CONFIG,
                                                    retain_days=7,  # How long to keep old working directories
                                                    monitor_days=2  # How many days of history to check for status 
                                                  )


def spawn_text_stats_pipe(text: str,spawn_mode:Literal["in-process","luigid"] ="in-process", factory: Optional[ToToPipeFactory] = THE_PIPE_FACTORY) -> SampleWordStatsPipe:
    """Convenience function to make a specific kind of pipe and spawn it.
    
    Rather than using pipe factory directly, you will typically want to create a convenience function.
    This function will make a pipe, bind inputs to it, and spawn it.

    NOTE: It's a good idea to have the spawn_mode be a parameter.
    The 'in-process' mode is easier to troubleshoot.
    The 'luigid' mode is better for production.
    """
    text_content = TextContent(text_content=text)
    pipe = factory.make(
        pipe_class_name=SampleWordStatsPipe.__name__,  # Use the class name string
        explicit_inputs={"input_text": text_content},
        uniq_src=f"sample_{datetime.now().strftime('%H%M%S')}"  # Optional: provide a meaningful source for the unique directory name
    )
    pipe.bind_inputs(
        explicit_inputs={"input_text": text_content},
        ignore_missing_inputs=True
    )
    pipe.spawn(mode=spawn_mode)
    return pipe

########################################################
# Code above this line is the example.  Code below this line
# is just the guts necessary to make the example work.
########################################################


def create_factory(root_dir: Optional[Path]=None) -> ToToPipeFactory:
    """Creates a ToToPipeFactory instance.
    
    This demonstrates the two ways to get a factory:
    1. Using the global factory (when root_dir is None)
    2. Creating a new factory with a custom root directory
    
    Args:
        root_dir: Optional custom root directory. If None, uses the global factory.
        
    Returns:
        Either the global factory instance or a new factory with the specified root.    
    """
    if root_dir is None:
        return THE_PIPE_FACTORY
        
    return ToToPipeFactory(
        pipe_tree_root=root_dir,
        config_obj=CONFIG,
        retain_days=7,  # How long to keep old working directories
        monitor_days=2  # How many days of history to check for status 
    )

def launch_word_stats_pipe(factory: ToToPipeFactory, text: str) -> Path:
    """Demonstrates how to use a factory to create and launch a pipe.
    
    This shows the key factory usage pattern:
    1. Use factory.make() to create a new pipe instance
    2. Bind inputs to the pipe
    3. Spawn the pipe for execution
    """
    # Create a new pipe instance using the factory
    # The factory handles:
    # - Creating a unique working directory
    # - Setting up the directory structure
    # - Instantiating the pipe class
    text_content = TextContent(text_content=text)
    pipe = factory.make(
        pipe_class_name=SampleWordStatsPipe.__name__,  # Use the class name string
        explicit_inputs={"input_text": text_content},
        uniq_src=f"sample_{datetime.now().strftime('%H%M%S')}"  # Optional: provide a meaningful source for the unique directory name
    )
    pipe.bind_inputs(
        explicit_inputs={"input_text": text_content},
        ignore_missing_inputs=True
    )
    pipe.spawn()
    return pipe.working_dir

async def display_pipe_stats(factory: ToToPipeFactory) -> None:
    """Demonstrates how to use factory.pipe_stats() to monitor pipes.
    
    Shows how to:
    1. Get status of all pipes
    2. Filter for recently completed pipes
    3. Sort and display results
    """
    # Get all pipe stats and sort them
    all_stats = list(factory.pipe_stats())
    all_stats.sort()  # Uses the PipeStat comparison methods
    
    print("\nAll Pipes:")
    print("-" * 80)
    for stat in all_stats:
        print(f"Pipe {stat.pipe_class_name} is in state {stat.state}")
        print(f"State: {stat.state}")
        print(f"Created: {stat._determine_create_date}")
        print(f"Last Modified: {stat.last_modified}")
        print(f"Working Dir: {stat.working_dir}")
        print("-" * 80)
    
    # Demonstrate getting recently completed pipes
    one_hour_ago = datetime.now() - timedelta(hours=1)
    recent = list(factory.recently_completed(one_hour_ago))
    if recent:
        print("\nRecently Completed Pipes:")
        print("-" * 80)
        for stat in recent:
            print(f"Class: {stat.pipe_class_name}")
            print(f"Completed: {stat.last_modified}")
            print(f"Working Dir: {stat.working_dir}")
            print("-" * 80)

@click.group()
@click.option('--root-dir', '-r', 
              type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
              required=False,
              help='Optional root directory for pipe instances. If not provided, uses the global factory.')
@click.pass_context
def cli(ctx: click.Context, root_dir: Optional[Path]):
    """Demonstrates ToToPipeFactory usage with a sample word statistics pipe."""
    # Create factory using either provided root_dir or global factory
    ctx.obj = create_factory(root_dir)

@cli.command()
@click.argument('text')
@click.pass_obj
def analyze(factory: ToToPipeFactory, text: str):
    """Create and launch a word stats pipe to analyze TEXT."""
    working_dir = launch_word_stats_pipe(factory, text)
    print(f"Launched pipe in: {working_dir}")
    print("Use the 'status' command to monitor pipe execution.")

@cli.command()
@click.pass_obj
def status(factory: ToToPipeFactory):
    """Display status of all pipes in the factory."""
    asyncio.run(display_pipe_stats(factory))

if __name__ == '__main__':
    cli()
