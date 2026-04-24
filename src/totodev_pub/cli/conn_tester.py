#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
conn_tester - Multi-Connector CLI for connectivity testing

A command-line tool for verifying connectivity and permissions to third-party systems
while maintaining a structured, human-readable record of each testing attempt.

## Purpose and Design Philosophy

This tool addresses a common practical reality: few people know which connection information
is necessary to set up a connection, and even fewer are able to verify that the connection
information is missing or broken. Solving problems in this area often requires bringing
together cross-organizational experts in ad-hoc meetings that can take days to arrange.

conn_tester is designed for **initial setup and testing of connection information provided
by clients or vendors**. It provides quick validation of client-provided endpoints and
credentials with actionable diagnostics, while maintaining structured record-keeping via
append-only YAML logfiles to avoid repeated guesswork.

This approach is particularly valuable when:

- **Validating client-provided connection details** (endpoints, credentials, ports)
- **Testing vendor-supplied connection information** before integration
- **Documenting connection setup attempts** for troubleshooting sessions
- **Providing actionable diagnostics** when connections fail
- **Avoiding repeated guesswork** through structured record-keeping

## Core Workflow: Connection Validation

The tool is designed to validate connection information provided by clients or vendors,
running tests repeatedly against the same logfile until the connection succeeds.
Each run appends a new trial record to the logfile, creating a chronological history
of validation attempts, failures, and eventual success.

### Typical Usage Pattern:

1. **Receive Connection Info**: Client/vendor provides connection details (host, port, credentials)

2. **Initial Validation**: Test the provided connection information
   ```bash
   python conn_tester.py https --url https://api.client.com/ -f validation.yaml
   ```

3. **Identify Issues**: Review diagnostic output to understand what's missing or incorrect

4. **Request Corrections**: Work with client/vendor to fix identified issues

5. **Re-test with Changes**: Document what was corrected and test again
   ```bash
   python conn_tester.py https --url https://api.client.com/ -f validation.yaml \
     --change-note "Client updated SSL certificate"
   ```

6. **Repeat Until Success**: Continue the cycle until connection is validated

7. **Document Results**: The logfile provides a complete audit trail for handoff

## Logfile Structure

Each logfile tracks:
- **Metadata**: Test type, description, system environment, start time
- **Trial Runs**: Sequential attempts with timestamps, parameters, and results
- **Error Details**: Specific failure reasons and remediation advice
- **Change Tracking**: Notes about what was modified between runs

## Usage Examples:

Basic connection validation:
    python conn_tester.py list
    python conn_tester.py http --url http://client-api.example.com/ -f validation.yaml
    python conn_tester.py https --url https://vendor-service.com/ -f validation.yaml

Client/vendor connection validation with change tracking:
    python conn_tester.py https --url https://client-api.service.com/ -f validation.yaml \
      --change-note "Client provided updated SSL certificate" \
      --experiment-description "Client API connectivity validation for integration"

## Key Features

- **Prerequisite Testing**: Automatically runs DNS, TCP, and TLS checks before main tests
- **Structured Output**: YAML logfiles for easy parsing and analysis
- **Error Classification**: Categorized error types with specific remediation advice
- **Security**: Automatic credential redaction in logfiles
- **Extensible**: Plugin architecture for custom test types

## Developer Guide: Adding New Test Types

If you want to add a new test type, you can create a new file in the `conn_tester_support/test_plugins/readme-creating-conntests.md` for more info.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Type

import click

# Add current directory to Python path for module imports
sys.path.insert(0, str(Path(__file__).parent))

from conn_tester_support.logfile_manager import load_and_merge_parameters, execute_test_with_logfile
from conn_tester_support.test_plugins import discover_available_tests, load_test_class

# Private test types that should not be exposed as CLI commands
PRIVATE_CONNTESTS = ['dns', 'http_responds', 'https_responds', 'ssh_responds']


# =============================================================================
# Confidential Parameter Helpers
# =============================================================================


def _discover_confidential_fields() -> Dict[str, List[str]]:
    fields: Dict[str, List[str]] = {}
    plugin_dir = Path(__file__).parent / "conn_tester_support" / "test_plugins"

    try:
        available_tests = discover_available_tests(plugin_dir)
    except Exception:
        return fields

    for test_name in available_tests:
        if test_name in PRIVATE_CONNTESTS:
            continue
        try:
            test_class = load_test_class(test_name)
            metadata = test_class.describe_self()
            fields[test_name] = metadata.confidential_fields
        except Exception:
            continue

    return fields


CONFIDENTIAL_FIELDS_BY_TEST = _discover_confidential_fields()


def _emit_confidential_error(test_name: str, option_name: str, env_var: str) -> None:
    click.echo("", err=True)
    click.echo("Confidential parameter detected in CLI arguments", err=True)
    click.echo(f"Test type: {test_name}", err=True)
    click.echo(f"Forbidden option: --{option_name}", err=True)
    click.echo("", err=True)
    click.echo("Confidential parameters must be supplied via environment variables, not CLI arguments.", err=True)
    click.echo("Please set the environment variable instead:", err=True)
    click.echo(f"  export {env_var}='your-secret-value'", err=True)
    click.echo(f"  python conn_tester.py {test_name} ...", err=True)
    click.echo("", err=True)
    click.echo("Reason: CLI arguments are visible in process listings and shell history, which can leak secrets.", err=True)
    raise SystemExit(2)


def _check_confidential_cli_args(argv: List[str]) -> None:
    if len(argv) < 2:
        return

    # Determine test type (first non-option argument)
    test_type = None
    for token in argv[1:]:
        if token.startswith('-'):
            continue
        test_type = token
        break

    if not test_type:
        return

    confidential_fields = CONFIDENTIAL_FIELDS_BY_TEST.get(test_type, [])
    if not confidential_fields:
        return

    confidential_upper = {field.upper() for field in confidential_fields}

    for token in argv[1:]:
        if not token.startswith('--'):
            continue

        # Support --option=value syntax
        option_part = token[2:]
        if '=' in option_part:
            option_part = option_part.split('=', 1)[0]

        candidate = option_part.replace('-', '_').upper()
        if candidate in confidential_upper:
            env_var = next(field for field in confidential_fields if field.upper() == candidate)
            _emit_confidential_error(test_type, option_part, env_var)


# ============================================================================
# Click CLI
# ============================================================================

@click.group()
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose output')
@click.option('--silent', '-s', is_flag=True, help='Suppress all output')
@click.pass_context
def cli(ctx, verbose, silent):
    """conn-tester - Multi-Connector CLI for connectivity testing"""
    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose
    ctx.obj['silent'] = silent


@cli.command()
def list():
    """List all available test types"""
    # Use filesystem discovery to find available test files
    from pathlib import Path
    
    plugin_dir = Path(__file__).parent / "conn_tester_support" / "test_plugins"
    available_tests = discover_available_tests(plugin_dir)
    
    click.echo("Available test types:")
    for test_name in available_tests:
        # Skip private tests
        if test_name in PRIVATE_CONNTESTS:
            continue
            
        # Load the test class only to get its description
        try:
            test_class = load_test_class(test_name)
            metadata = test_class.describe_self()
            click.echo(f"  {test_name}: {metadata.description}")
        except Exception as e:
            click.echo(f"  {test_name}: (error loading: {e})")


@cli.command()
def jsonlist():
    """List all available test types in JSON format for programmatic consumption"""
    import json
    from pathlib import Path
    
    plugin_dir = Path(__file__).parent / "conn_tester_support" / "test_plugins"
    available_tests = discover_available_tests(plugin_dir)
    
    test_info = {}
    
    for test_name in available_tests:
        # Skip private tests
        if test_name in PRIVATE_CONNTESTS:
            continue

        # Load the test class to get its metadata
        try:
            test_class = load_test_class(test_name)
            metadata = test_class.describe_self()

            test_info[test_name] = {
                "description": metadata.description,
                "config_fields": metadata.config_fields,
                "required_fields": metadata.required_fields,
                "optional_fields": metadata.optional_fields,
                "confidential_fields": metadata.confidential_fields
            }
        except Exception as e:
            # Surface plugin load failures in a structured way so that
            # callers (e.g., the web UI) can display a clear message.
            # We intentionally use the phrase "internal error" for users
            # rather than implying the problem is temporary.
            error_str = f"{e.__class__.__name__}: {e}"
            test_info[test_name] = {
                "status": "internal_error",
                "description": f"{test_name} test (internal error)",
                "user_message": (
                    "This test cannot be run because of an internal error loading its configuration. "
                    "Please contact support with this information."
                ),
                "debug_error": error_str,
                # Retain legacy key for backward compatibility
                "error": f"Failed to load test: {error_str}",
            }
    
    # Output as JSON
    click.echo(json.dumps(test_info, indent=2))


@cli.command()
@click.argument('logfile', type=click.Path(exists=True))
@click.option('--run', '-r', help='Specific run ID to show details for (e.g., 001, 002)')
@click.option('--json', '-j', is_flag=True, help='Output in JSON format')
def view(logfile, run, json):
    """View detailed results from a logfile"""
    import json
    from pathlib import Path
    from conn_tester_support.logfile_manager import LogfileManager
    
    try:
        manager = LogfileManager(logfile)
        manager.load_from_yaml()
        
        if not manager.experiment or not manager.experiment.runs:
            click.echo("No runs found in logfile", err=True)
            return
        
        if run:
            # Show specific run
            if run not in manager.experiment.runs:
                click.echo(f"Run {run} not found in logfile", err=True)
                return
            
            trial_run = manager.experiment.runs[run]
            
            if json:
                # Output as JSON
                output = {
                    "run_id": run,
                    "is_success": trial_run.is_success,
                    "run_time": trial_run.run_time.isoformat(),
                    "run_changes": trial_run.run_changes,
                    "run_params": trial_run.run_params,
                    "extra_detail": trial_run.extra_detail
                }
                if trial_run.experiment_error:
                    output["experiment_error"] = {
                        "short": trial_run.experiment_error.short,
                        "long": trial_run.experiment_error.long,
                        "advice": trial_run.experiment_error.advice
                    }
                click.echo(json.dumps(output, indent=2))
            else:
                # Human-readable output
                click.echo(f"Run {run}: {trial_run.run_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                click.echo(f"Status: {'✓ SUCCESS' if trial_run.is_success else '✗ FAILED'}")
                
                if trial_run.run_changes:
                    click.echo(f"Changes: {trial_run.run_changes}")
                
                if trial_run.experiment_error:
                    click.echo(f"Error: {trial_run.experiment_error.short}")
                    if trial_run.experiment_error.long:
                        click.echo(f"Details: {trial_run.experiment_error.long}")
                    if trial_run.experiment_error.advice:
                        click.echo("Advice:")
                        for advice_line in trial_run.experiment_error.advice.strip().split('\n'):
                            if advice_line.strip():
                                click.echo(f"  - {advice_line.strip()}")
                
                # Show extra_detail if available
                if trial_run.extra_detail:
                    click.echo("\nDetailed Results:")
                    click.echo(json.dumps(trial_run.extra_detail, indent=2))
        else:
            # Show summary of all runs
            if json:
                output = {
                    "experiment_type": manager.experiment.conn_test.experiment_type,
                    "experiment_description": manager.experiment.conn_test.experiment_description,
                    "runs": {}
                }
                for run_id, trial_run in manager.experiment.runs.items():
                    output["runs"][run_id] = {
                        "is_success": trial_run.is_success,
                        "run_time": trial_run.run_time.isoformat(),
                        "run_changes": trial_run.run_changes,
                        "has_extra_detail": bool(trial_run.extra_detail)
                    }
                click.echo(json.dumps(output, indent=2))
            else:
                click.echo(f"Experiment: {manager.experiment.conn_test.experiment_type}")
                click.echo(f"Description: {manager.experiment.conn_test.experiment_description}")
                click.echo(f"Total runs: {len(manager.experiment.runs)}")
                click.echo("\nRuns:")
                for run_id, trial_run in manager.experiment.runs.items():
                    status = "✓ SUCCESS" if trial_run.is_success else "✗ FAILED"
                    has_details = " (has details)" if trial_run.extra_detail else ""
                    click.echo(f"  {run_id}: {trial_run.run_time.strftime('%Y-%m-%d %H:%M:%S UTC')} - {status}{has_details}")
                click.echo(f"\nUse 'conn-tester view {logfile} --run <run_id>' to see detailed results for a specific run")
                
    except Exception as e:
        click.echo(f"Error reading logfile: {e}", err=True)


class ConfidentialFieldCommand(click.Command):
    """Custom Click command that provides clearer errors for confidential fields"""

    def __init__(self, *args, confidential_fields=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.confidential_fields = confidential_fields or []

    def make_context(self, *args, **kwargs):
        try:
            return super().make_context(*args, **kwargs)
        except click.exceptions.NoSuchOption as exc:
            self._maybe_handle_confidential_option(exc)
            raise
        except click.exceptions.UsageError as exc:
            if "No such option" in str(exc):
                self._maybe_handle_confidential_option(exc)
            raise

    def _maybe_handle_confidential_option(self, exc: Exception) -> None:
        import re

        option_name = getattr(exc, "option_name", None)
        if option_name is None:
            match = re.search(r"--([a-z0-9-]+)", str(exc))
            if match:
                option_name = match.group(1)

        if not option_name:
            return

        env_var_candidate = option_name.replace('-', '_').upper()
        for conf_field in self.confidential_fields:
            if conf_field.upper() == env_var_candidate:
                click.echo("", err=True)
                click.echo("Confidential parameter detected in CLI arguments", err=True)
                click.echo(f"Parameter: --{option_name}", err=True)
                click.echo("", err=True)
                click.echo("Confidential parameters must be supplied via environment variables for security.", err=True)
                click.echo("Please set the environment variable instead:", err=True)
                click.echo(f"  export {conf_field}='your-secret-value'", err=True)
                click.echo(f"  python conn_tester.py {self.name} ...", err=True)
                click.echo("", err=True)
                click.echo("CLI arguments are visible in process listings and shell history, which can leak secrets.", err=True)
                raise SystemExit(2)


def create_dynamic_command(test_type: str):
    """Create a dynamic Click command for a test type with lazy loading"""

    preview_test_class = None
    preview_metadata = None
    try:
        preview_test_class = load_test_class(test_type)
        preview_metadata = preview_test_class.describe_self()
        confidential_fields_for_cmd = preview_metadata.confidential_fields
    except Exception:
        confidential_fields_for_cmd = []
    
    @click.command(name=test_type, cls=ConfidentialFieldCommand, confidential_fields=confidential_fields_for_cmd)
    @click.option('--file', '-f', required=True, help='Logfile path (use - for STDOUT)')
    @click.option('--change-note', help='Description of changes for this run')
    @click.option('--note', help='User note to add to this run (e.g., "client reset DNS", "updated credentials")')
    @click.option('--experiment-description', help='Description of the overall experiment')
    @click.option('--verbose', '-v', is_flag=True, help='Enable verbose output')
    @click.option('--silent', '-s', is_flag=True, help='Suppress all output')
    @click.pass_context
    def dynamic_test_command(ctx, file, change_note, note, experiment_description, verbose, silent, **kwargs):
        """Dynamic test command created from test type"""
        # Remove Click-specific options from config
        cli_params = {k: v for k, v in kwargs.items() if k not in ['file', 'change_note', 'note', 'experiment_description', 'verbose', 'silent']}
        
        # Lazy load the test class only when this command is invoked
        try:
            test_class = preview_test_class or load_test_class(test_type)
        except ValueError as e:
            click.echo(f"Error loading test '{test_type}': {e}", err=True)
            sys.exit(1)
        
        # Load and merge parameters from logfile
        merged_config, param_changes = load_and_merge_parameters(file, test_type, cli_params, note)
        
        # Create test instance with merged configuration
        test = test_class(merged_config)
        
        # Format run changes with parameter changes and note if provided
        run_changes_parts = []
        if change_note:
            run_changes_parts.append(change_note)
        if param_changes:
            run_changes_parts.append(f"Parameter changes: {param_changes}")
        if note:
            run_changes_parts.append(note)
        
        run_changes = " ".join(run_changes_parts) if run_changes_parts else None
        
        # Execute test with logfile handling
        execute_test_with_logfile(
            test=test,
            experiment_type=test_type,
            experiment_description=experiment_description,
            file_path=file,
            change_note=run_changes,
            verbose=verbose,
            silent=silent
        )
    
    # Get test metadata to add dynamic options
    try:
        test_class = preview_test_class or load_test_class(test_type)
        metadata = preview_metadata or test_class.describe_self()
        
        # Add test-specific options based on TestMetadata
        all_fields = metadata.config_fields
        confidential_fields_lower = [f.lower() for f in metadata.confidential_fields]
        
        for field_name, description in all_fields.items():
            # Skip confidential fields - they should only be passed via environment variables
            if field_name.lower() in confidential_fields_lower:
                continue
            
            # Convert field name to CLI option name
            option_name = field_name.replace('_', '-')
            
            # All parameters are now optional since they can be loaded from logfile
            # Add the option to the command
            click.option(f'--{option_name}', required=False, help=f"{description} (can be loaded from logfile)")(dynamic_test_command)
        
        # Add confidential fields information to the command's help text
        if metadata.confidential_fields:
            # Get the original docstring and append confidential fields info
            original_doc = dynamic_test_command.help or "Dynamic test command created from test type"
            
            confidential_info = "\n\nConfidential fields (must be set via environment variables):\n"
            for field in metadata.confidential_fields:
                confidential_info += f"  {field}\n"
            
            dynamic_test_command.help = original_doc + confidential_info
            
    except ValueError:
        # If we can't load the test class, create a basic command without dynamic options
        pass
    
    return dynamic_test_command


def register_dynamic_commands():
    """Register dynamic commands for all public test types"""
    from pathlib import Path
    
    # Discover available test files without importing them
    plugin_dir = Path(__file__).parent / "conn_tester_support" / "test_plugins"
    available_tests = discover_available_tests(plugin_dir)
    
    for test_type in available_tests:
        # Skip private tests
        if test_type in PRIVATE_CONNTESTS:
            continue
            
        # Create dynamic command for public tests
        try:
            dynamic_command = create_dynamic_command(test_type)
            cli.add_command(dynamic_command)
        except Exception as e:
            # Skip tests that can't be loaded
            pass


# ============================================================================
# Main Entry Point
# ============================================================================

# Register dynamic commands for all test types
register_dynamic_commands()

if __name__ == '__main__':
    _check_confidential_cli_args(sys.argv)
    cli()
