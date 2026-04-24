#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Logfile management for conn_tester.

This module handles loading, saving, and managing logfiles (formerly called experiment files)
that track connection test attempts and results.
"""

from __future__ import annotations

import os
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional

import click
import yaml

from .models import (
    ConnTest, ExperimentError, ExperimentFile, TrialRun
)
from .core import get_local_environment, ConfigurationError, _hash_credential


# ============================================================================
# Logfile Manager
# ============================================================================

class LogfileManager:
    """Manages logfile operations (renamed from ExperimentFileManager)"""
    
    def __init__(self, file_path: Optional[str] = None):
        self.file_path = file_path
        self.experiment: Optional[ExperimentFile] = None
    
    def load_from_yaml(self) -> ExperimentFile:
        """Load logfile from YAML file"""
        if not self.file_path or self.file_path == "-":
            raise ValueError("Cannot load from STDOUT")
        
        try:
            with open(self.file_path, 'r') as f:
                data = yaml.safe_load(f)
            
            self.experiment = ExperimentFile(**data)
            return self.experiment
        except FileNotFoundError:
            raise FileNotFoundError(f"Logfile not found: {self.file_path}")
        except Exception as e:
            raise ValueError(f"Invalid logfile: {e}")
    
    def save_to_yaml(self, experiment: ExperimentFile) -> None:
        """Save logfile to YAML file"""
        # Convert to dict with proper field ordering
        data = experiment.model_dump()
        
        # Suppress logfile_version when it's 1 (default)
        if data.get('logfile_version') == 1:
            del data['logfile_version']
        
        # Reorder conn_test fields to put started_at before local_environment
        if 'conn_test' in data and isinstance(data['conn_test'], dict):
            conn_test = data['conn_test']
            # Create ordered dict with desired field order, then convert to regular dict
            ordered_conn_test = OrderedDict([
                ('experiment_type', conn_test.get('experiment_type')),
                ('experiment_description', conn_test.get('experiment_description')),
                ('experiment_detail', conn_test.get('experiment_detail')),
                ('started_at', conn_test.get('started_at')),
                ('local_environment', conn_test.get('local_environment'))
            ])
            # Convert to regular dict to avoid YAML tags
            data['conn_test'] = dict(ordered_conn_test)
        
        if self.file_path == "-":
            # Output to STDOUT
            print(yaml.dump(data, default_flow_style=False, sort_keys=False))
        else:
            with open(self.file_path, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    
    def get_next_run_id(self) -> str:
        """Get next run ID in sequence (001, 002, 003, etc.)"""
        if not self.experiment:
            return "001"
        
        existing_runs = [int(k) for k in self.experiment.runs.keys() if k.isdigit()]
        if not existing_runs:
            return "001"
        
        next_num = max(existing_runs) + 1
        return f"{next_num:03d}"
    
    def append_run(self, trial_run: TrialRun) -> None:
        """Append a new run to the logfile"""
        if not self.experiment:
            raise ValueError("No logfile loaded")
        
        run_id = self.get_next_run_id()
        self.experiment.runs[run_id] = trial_run
    
    def format_run_summary(self, run_id: str, trial_run: TrialRun) -> str:
        """Format a trial run for display to user"""
        lines = []
        
        # Only show run number if it's not the first run (001)
        if run_id == "001":
            lines.append(f"{trial_run.run_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        else:
            lines.append(f"Run {run_id}: {trial_run.run_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        if trial_run.run_changes:
            lines.append(f"Changes: {trial_run.run_changes}")
        
        if trial_run.is_success:
            lines.append("✓ SUCCESS")
        else:
            lines.append("✗ FAILED")
            if trial_run.experiment_error:
                lines.append(f"Error: {trial_run.experiment_error.short}")
                if trial_run.experiment_error.long:
                    lines.append(f"Details: {trial_run.experiment_error.long}")
                if trial_run.experiment_error.advice:
                    advice_lines = trial_run.experiment_error.advice.strip().split('\n')
                    lines.append("Advice:")
                    for advice in advice_lines:
                        if advice.strip():
                            lines.append(f"  - {advice.strip()}")
        
        return "\n".join(lines)


# ============================================================================
# Helper Functions
# ============================================================================

def load_and_merge_parameters(
    file_path: str,
    experiment_type: str,
    cli_params: Dict[str, Any],
    note: Optional[str] = None
) -> tuple[Dict[str, Any], Optional[str]]:
    """
    Load parameters from existing logfile and merge with CLI parameters.
    
    Special value '~' (tilde) can be used to explicitly exclude a parameter:
    1. The parameter is NOT passed to the plugin (plugin uses its default)
    2. The parameter is NOT loaded from the logfile
    3. If ANY parameter contains '~', the duplicate run check is disabled
       (user is explicitly indicating they want to run with different params)
    
    Args:
        file_path: Path to logfile
        experiment_type: Type of experiment to validate against
        cli_params: Parameters provided via command line (use '~' to exclude)
        note: User note (required if config is identical to last run)
        
    Returns:
        Tuple of (merged parameters, change description)
    """
    if file_path == "-" or not Path(file_path).exists():
        # No existing file, use only CLI params
        return cli_params, None
    
    try:
        manager = LogfileManager(file_path)
        experiment = manager.load_from_yaml()
        
        # Validate experiment type matches
        if experiment.conn_test.experiment_type != experiment_type:
            raise ValueError(f"Logfile is for '{experiment.conn_test.experiment_type}' tests, but running '{experiment_type}' test")
        
        # Get parameters from the last run
        if not experiment.runs:
            # No previous runs, use only CLI params
            return cli_params, None
        
        # Get the last run (highest numbered run)
        last_run_id = max(experiment.runs.keys())
        last_run = experiment.runs[last_run_id]
        file_params = last_run.run_params or {}
        
        # Filter out None/null values from file params
        filtered_file_params = {k: v for k, v in file_params.items() if v is not None}
        
        # Process CLI params and handle exclusion markers ('~')
        has_tilde = False           # Flag: any parameter has ~
        filtered_cli_params = {}    # Excludes ~ (for plugin execution)
        excluded_params = set()     # Parameters marked with ~
        
        for k, v in cli_params.items():
            if v is None:
                continue
            if str(v).strip() == '~':
                # Mark that we have a tilde - this will disable duplicate run check
                has_tilde = True
                # Mark as excluded - don't pass to plugin, don't load from file
                excluded_params.add(k)
                continue
            # Regular parameter
            filtered_cli_params[k] = v
        
        # Remove excluded parameters from file params so they don't get merged
        filtered_file_params = {k: v for k, v in filtered_file_params.items() if k not in excluded_params}
        
        # Merge parameters (CLI params override file params)
        # Note: ~ values are NOT in filtered_cli_params, so they won't appear in merged_params
        merged_params = {**filtered_file_params, **filtered_cli_params}
        
        # Detect parameter changes
        changed_params = []
        for key, new_value in filtered_cli_params.items():
            old_value = filtered_file_params.get(key)
            if old_value != new_value:
                changed_params.append(key)
        
        # Check if the merged config is identical to the last run
        # Skip this check if user provided ~ for any parameter (they're explicitly changing something)
        if not has_tilde and filtered_file_params:
            # For accurate duplicate detection, we need to include confidential fields from environment variables
            # These are not in cli_params but may have changed between runs
            try:
                # DEBUG: Temporary logging
                if os.getenv('CONN_TESTER_DEBUG'):
                    click.echo(f"DEBUG: Starting duplicate detection with env vars", err=True)
                    click.echo(f"DEBUG: merged_params = {merged_params}", err=True)
                    click.echo(f"DEBUG: filtered_file_params = {filtered_file_params}", err=True)
                
                from .test_plugins import load_test_class
                test_class = load_test_class(experiment_type)
                metadata = test_class.describe_self()
                
                if os.getenv('CONN_TESTER_DEBUG'):
                    click.echo(f"DEBUG: confidential_fields = {metadata.confidential_fields}", err=True)
                
                # Create copies for comparison that include hashed confidential values
                current_params_with_env = merged_params.copy()
                previous_params_with_env = filtered_file_params.copy()
                
                if os.getenv('CONN_TESTER_DEBUG'):
                    click.echo(f"DEBUG: About to process {len(metadata.confidential_fields)} confidential fields", err=True)
                
                # Add hashed environment variable values for confidential fields
                for env_var_name in metadata.confidential_fields:
                    if os.getenv('CONN_TESTER_DEBUG'):
                        click.echo(f"DEBUG: Processing env_var: {env_var_name}", err=True)
                    # Map environment variable name to parameter name
                    # E.g., GMAIL_PRIVATE_KEY -> private_key, MS365_CLIENT_SECRET -> client_secret
                    # Remove test-type prefix and convert to lowercase
                    param_name = env_var_name.lower()
                    # Remove common prefixes like 'gmail_', 'ms365_', 'sharepoint_', 'ssh_'
                    for prefix in [f'{experiment_type}_', 'gmail_', 'ms365_', 'sharepoint_', 'ssh_']:
                        if param_name.startswith(prefix):
                            param_name = param_name[len(prefix):]
                            break
                    
                    # Check if this confidential field has a value in the environment
                    if env_var_name in os.environ:
                        current_value = os.environ[env_var_name]
                        current_hash = _hash_credential(current_value)
                        # Replace the stale hash from the logfile with the current hash
                        current_params_with_env[param_name] = current_hash
                        
                        # The previous run's params already have the hashed value stored
                        # (if the field was present), so we keep it as-is in previous_params_with_env
                    else:
                        # No environment variable provided - remove from current params if present
                        # (This happens when the confidential field was in the logfile but not in environment)
                        current_params_with_env.pop(param_name, None)
                
                # Now compare including the confidential fields
                # DEBUG: Temporary logging
                import os as os_debug
                if os_debug.getenv('CONN_TESTER_DEBUG'):
                    click.echo(f"DEBUG: current_params_with_env = {current_params_with_env}", err=True)
                    click.echo(f"DEBUG: previous_params_with_env = {previous_params_with_env}", err=True)
                    click.echo(f"DEBUG: Are they equal? {current_params_with_env == previous_params_with_env}", err=True)
                
                if current_params_with_env == previous_params_with_env:
                    # Identical configuration - require a note
                    if not note or not note.strip():
                        click.echo("Error: This test configuration is identical to the last run.", err=True)
                        click.echo("Please provide a --note explaining what changed or why you're rerunning this test.", err=True)
                        click.echo("Example: --note 'client updated DNS' or --note 'checking after maintenance'", err=True)
                        sys.exit(1)
                    return merged_params, None
                else:
                    # Check if only confidential fields changed
                    for env_var_name in metadata.confidential_fields:
                        # Use same mapping logic as above
                        param_name = env_var_name.lower()
                        for prefix in [f'{experiment_type}_', 'gmail_', 'ms365_', 'sharepoint_', 'ssh_']:
                            if param_name.startswith(prefix):
                                param_name = param_name[len(prefix):]
                                break
                        
                        if param_name in current_params_with_env and param_name in previous_params_with_env:
                            if current_params_with_env[param_name] != previous_params_with_env[param_name]:
                                # Confidential field changed - add to changed_params list
                                if param_name not in changed_params:
                                    changed_params.append(param_name)
                        elif param_name in current_params_with_env and param_name not in previous_params_with_env:
                            # New confidential field added
                            if param_name not in changed_params:
                                changed_params.append(param_name)
            
            except Exception as e:
                # If we can't load test metadata, fall back to simple comparison (backward compatible)
                if os.getenv('CONN_TESTER_DEBUG'):
                    click.echo(f"DEBUG: Exception in duplicate detection: {e}", err=True)
                    import traceback
                    traceback.print_exc()
                
                if merged_params == filtered_file_params:
                    # Identical configuration - require a note
                    if not note or not note.strip():
                        click.echo("Error: This test configuration is identical to the last run.", err=True)
                        click.echo("Please provide a --note explaining what changed or why you're rerunning this test.", err=True)
                        click.echo("Example: --note 'client updated DNS' or --note 'checking after maintenance'", err=True)
                        sys.exit(1)
                    return merged_params, None
        
        # Create change description
        change_description = None
        if changed_params:
            change_description = f"[{', '.join(changed_params)}]"
        elif has_tilde and excluded_params:
            # If we have tilde but no other changes, note which parameters were excluded
            change_description = f"[excluded: {', '.join(sorted(excluded_params))}]"
        
        return merged_params, change_description
        
    except Exception as e:
        click.echo(f"Warning: Could not load parameters from logfile: {e}", err=True)
        return cli_params, None


def execute_test_with_logfile(
    test,
    experiment_type: str,
    experiment_description: Optional[str],
    file_path: Optional[str],
    change_note: Optional[str],
    verbose: bool,
    silent: bool
) -> None:
    """
    Execute a test and handle logfile operations.
    
    Args:
        test: The test instance to run
        experiment_type: Type of experiment (http, https, etc.)
        experiment_description: Optional description for the experiment
        file_path: Path to logfile (None for no file, "-" for STDOUT)
        change_note: Optional note about changes for this run
        verbose: Whether to show verbose output
        silent: Whether to suppress all output
    """
    if silent:
        return
    
    # Validate that file_path is provided (required for logfile tracking)
    if file_path is None:
        click.echo("Error: Output file is required. Use -f <filename> to specify a logfile, or -f - for STDOUT.", err=True)
        click.echo("Example: conn-tester http --url http://example.com/ -f logfile.yaml", err=True)
        sys.exit(1)
    
    # Run test
    if verbose:
        click.echo(f"Running {experiment_type.upper()} test...")
    
    # Create logger for verbose output
    logger = None
    if verbose:
        import logging
        logger = logging.getLogger(f"conn_tester.{experiment_type}")
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
    
    try:
        result = test.run_test(logger=logger)
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        if e.errors:
            click.echo("Required fields:", err=True)
            for error in e.errors:
                click.echo(f"  - {error}", err=True)
        sys.exit(1)
    
    # Create trial run for logfile
    trial_run = TrialRun(
        is_success=result.success,
        run_changes=change_note,
        run_params=test.get_configs(),
        experiment_error=ExperimentError(
            short=result.error_type,
            long=result.error_message,
            advice="\n".join(result.advice) if result.advice else None
        ) if not result.success else None,
        extra_detail=result.extra_detail  # Single source of truth for detailed results
    )
    
    # Handle logfile if specified
    if file_path:
        try:
            manager = LogfileManager(file_path)
            if file_path != "-" and Path(file_path).exists():
                manager.load_from_yaml()
            else:
                # Create new experiment
                description = experiment_description or test.describe_self().description
                conn_test = ConnTest(
                    experiment_type=experiment_type,
                    experiment_description=description,
                    local_environment=get_local_environment()
                )
                manager.experiment = ExperimentFile(conn_test=conn_test)
            
            manager.append_run(trial_run)
            manager.save_to_yaml(manager.experiment)
            
            # Display run summary
            run_id = manager.get_next_run_id()
            if run_id == "001":
                run_id = "001"  # First run
            else:
                # Get the actual run ID that was just added
                run_id = str(int(run_id) - 1).zfill(3)
            
            click.echo("\n" + "="*50)
            click.echo("LOGFILE RUN RECORD")
            click.echo("="*50)
            click.echo(manager.format_run_summary(run_id, trial_run))
            click.echo("="*50)
            
        except Exception as e:
            click.echo(f"Warning: Could not save to logfile: {e}", err=True)
    
    # Display immediate result with shortname
    if result.success:
        click.echo(f"✓ {experiment_type.upper()} test successful")
    else:
        lines = [f"✗ {experiment_type.upper()} test failed: {result.error_message}"]
        if result.advice:
            lines.append(f"Advice: {'; '.join(result.advice)}")
        click.echo("\n".join(lines))
    
    if not result.success:
        sys.exit(1)
