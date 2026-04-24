#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Pydantic models and custom exceptions for conn_tester.

This module contains all the data models used by the connection testing system,
including test metadata, results, and logfile structures.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field, ConfigDict


# ============================================================================
# Custom Exceptions
# ============================================================================

class ConfigurationError(Exception):
    """Exception raised when test configuration is invalid"""
    def __init__(self, message: str, errors: List[str] = None):
        super().__init__(message)
        self.errors = errors or []


# ============================================================================
# Pydantic Models
# ============================================================================

class TestMetadata(BaseModel):
    """Metadata about a test type including description and configuration fields"""
    description: str
    config_fields: Dict[str, str] = Field(default_factory=dict)  # field_name -> description
    required_fields: List[str] = Field(default_factory=list)
    optional_fields: List[str] = Field(default_factory=list)
    confidential_fields: List[str] = Field(default_factory=list)  # fields that must be supplied via environment variables
    
    def get_field_description(self, field_name: str) -> Optional[str]:
        """Get description for a specific field"""
        return self.config_fields.get(field_name)
    
    def get_required_fields(self) -> Dict[str, str]:
        """Get only required fields with descriptions"""
        return {field: self.config_fields[field] for field in self.required_fields 
                if field in self.config_fields}
    
    def get_optional_fields(self) -> Dict[str, str]:
        """Get only optional fields with descriptions"""
        return {field: self.config_fields[field] for field in self.optional_fields 
                if field in self.config_fields}


class SystemInfo(BaseModel):
    """System information for local environment"""
    platform: Optional[str] = None
    python_version: Optional[str] = None
    os_type: Optional[str] = None
    
    model_config = ConfigDict(frozen=True)


class EgressCheck(BaseModel):
    """Egress connectivity check results"""
    dns_ok: bool
    tcp_ok: bool
    tls_ok: bool
    
    model_config = ConfigDict(frozen=True)


class LocalEnvironment(BaseModel):
    """Local environment information"""
    public_ip: Optional[str] = None
    system: SystemInfo = Field(default_factory=SystemInfo)
    egress_check: Optional[EgressCheck] = None


class ConnTest(BaseModel):
    """Connection test experiment metadata"""
    experiment_type: str  # e.g., "http", "https", "dns", "tcp"
    experiment_description: Optional[str] = None  # Human-readable description of the experiment
    experiment_detail: Dict[str, Any] = Field(default_factory=dict)  # connector-specific metadata
    started_at: datetime = Field(default_factory=datetime.utcnow)
    local_environment: LocalEnvironment = Field(default_factory=LocalEnvironment)


class ExperimentError(BaseModel):
    """Error details for failed test runs"""
    short: Optional[str] = None  # error code/type
    long: Optional[str] = None   # detailed error message
    advice: Optional[str] = None # remediation suggestions


class TrialRun(BaseModel):
    """Individual test run within an experiment"""
    is_success: bool
    run_changes: Optional[str] = None  # what was changed for this run
    run_params: Dict[str, Any] = Field(default_factory=dict)  # parameters used for this run
    run_time: datetime = Field(default_factory=datetime.utcnow)
    experiment_error: Optional[ExperimentError] = None  # omitted for successful runs
    extra_detail: Optional[Dict[str, Any]] = None  # detailed test results for both success and failure


class ExperimentFile(BaseModel):
    """Complete experiment file structure"""
    logfile_version: int = 1
    conn_test: ConnTest
    runs: Dict[str, TrialRun] = Field(default_factory=dict)  # keys like "001", "002", "003"


class TestResult(BaseModel):
    """Standardized test result matching ExperimentError structure"""
    __test__ = False  # Prevent pytest from collecting this as a test class
    success: bool
    error_type: Optional[str] = None  # Maps to standard error codes
    error_message: Optional[str] = None
    advice: List[str] = Field(default_factory=list)
    extra_detail: Dict[str, Any] = Field(default_factory=dict)  # Prerequisite test results and connector-specific details


class TestTypeInfo(BaseModel):
    """Information about a registered test type"""
    test_class_name: str  # Class name as string for lazy loading
    test_shortname: str
    is_public: bool = True
    description: Optional[str] = None
    _resolved_class: Optional[Type['TestTypeBase']] = None  # Cached resolved class
    
    model_config = ConfigDict(arbitrary_types_allowed=True)  # Allow Type[TestTypeBase]
    
    def get_test_class(self) -> Type['TestTypeBase']:
        """Resolve and return the actual test class"""
        if self._resolved_class is None:
            # Import the module and get the class
            module_name = self.test_class_name.split('.')[0]
            class_name = self.test_class_name.split('.')[-1]
            
            # For classes in the same module, we can use globals()
            if '.' not in self.test_class_name:
                # Simple case: class is in current module
                import sys
                current_module = sys.modules[__name__]
                self._resolved_class = getattr(current_module, class_name)
            else:
                # Complex case: class is in another module
                import importlib
                module = importlib.import_module(module_name)
                self._resolved_class = getattr(module, class_name)
        
        return self._resolved_class
