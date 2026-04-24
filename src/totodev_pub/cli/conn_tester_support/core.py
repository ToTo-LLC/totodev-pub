#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Core functionality for conn_tester including base classes, constants, and utilities.

This module contains the TestTypeBase abstract class, constants, and utility functions
used throughout the connection testing system.
"""

from __future__ import annotations

import hashlib
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type, Union

from .models import (
    ConfigurationError, TestMetadata, TestResult, TestTypeInfo,
    SystemInfo, EgressCheck, LocalEnvironment
)


# ============================================================================
# Constants
# ============================================================================

# Default timeouts (in seconds)
DEFAULT_DNS_TIMEOUT = 10
DEFAULT_TCP_TIMEOUT = 10
DEFAULT_HTTP_TIMEOUT = 30
DEFAULT_IP_LOOKUP_TIMEOUT = 5

# Default ports
HTTP_PORT = 80
HTTPS_PORT = 443
SSH_PORT = 22

# IP lookup service
IP_LOOKUP_SERVICE = 'https://api.ipify.org'

# Test connectivity targets
EGRESS_TEST_HOST = 'google.com'


# ============================================================================
# Utility Functions
# ============================================================================

def get_system_info() -> SystemInfo:
    """
    Gather system information including platform, Python version, and OS type.
    
    Returns:
        SystemInfo: Object containing system details
    """
    system_name = platform.system().lower()
    
    # Map platform.system() to common OS types
    os_type_mapping = {
        "darwin": "mac",
        "windows": "windows", 
        "linux": "linux"
    }
    os_type = os_type_mapping.get(system_name, system_name)
    
    return SystemInfo(
        platform=f"{platform.system()}-{platform.release()}",
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        os_type=os_type
    )


def get_public_ip() -> Optional[str]:
    """
    Get public IP address using external service.
    
    Returns:
        Optional[str]: Public IP address or None if lookup fails
    """
    try:
        with urllib.request.urlopen(IP_LOOKUP_SERVICE, timeout=DEFAULT_IP_LOOKUP_TIMEOUT) as response:
            return response.read().decode('utf-8').strip()
    except Exception:
        return None


def get_egress_check() -> EgressCheck:
    """
    Perform basic egress connectivity checks for DNS, TCP, and TLS.
    
    Returns:
        EgressCheck: Object containing connectivity test results
    """
    dns_ok = False
    tcp_ok = False
    tls_ok = False
    
    try:
        # DNS check
        socket.getaddrinfo(EGRESS_TEST_HOST, None)
        dns_ok = True
        
        # TCP check (port 80)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(DEFAULT_TCP_TIMEOUT)
            result = sock.connect_ex((EGRESS_TEST_HOST, HTTP_PORT))
            tcp_ok = (result == 0)
        
        # TLS check (port 443)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(DEFAULT_TCP_TIMEOUT)
            result = sock.connect_ex((EGRESS_TEST_HOST, HTTPS_PORT))
            tls_ok = (result == 0)
        
    except Exception:
        pass
    
    return EgressCheck(dns_ok=dns_ok, tcp_ok=tcp_ok, tls_ok=tls_ok)


def get_local_environment() -> LocalEnvironment:
    """
    Gather comprehensive local environment information.
    
    Returns:
        LocalEnvironment: Object containing system, network, and connectivity info
    """
    return LocalEnvironment(
        public_ip=get_public_ip(),
        system=get_system_info(),
        egress_check=get_egress_check()
    )


def _normalize_url_for_test(url: str, expected_protocol: str) -> str:
    """
    Normalize URL by ensuring it has the correct protocol prefix.
    
    Args:
        url: The URL to normalize
        expected_protocol: The expected protocol ('http' or 'https')
    
    Returns:
        str: Normalized URL with correct protocol
    """
    # If URL already has a protocol, use it as-is
    if url.startswith(('http://', 'https://')):
        return url
    
    # If no protocol, add the expected one
    return f"{expected_protocol}://{url}"


# ============================================================================
# TestTypeBase Abstract Class
# ============================================================================

class TestTypeBase(ABC):
    """Abstract base class for all test types"""
    
    # Class-level registry
    _test_registry: Dict[str, TestTypeInfo] = {}
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._last_result: Optional[TestResult] = None
    
    # Class Methods
    @classmethod
    @abstractmethod
    def describe_self(cls) -> TestMetadata:
        """Return test metadata including description and configuration fields"""
        pass
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        """
        Return list of prerequisite test classes that should run first.
        Common tests like DNS, TCP should be included here.
        """
        return []
    
    @staticmethod
    def contains_loopback(address_or_list_of_addresses: str) -> bool:
        """
        Check if a string contains loopback addresses that should be blocked.
        
        Args:
            address_or_list_of_addresses: String to check for loopback patterns
            
        Returns:
            True if loopback addresses are detected, False otherwise
        """
        if not address_or_list_of_addresses:
            return False
        
        # Convert to lowercase for case-insensitive matching
        text = address_or_list_of_addresses.lower().strip()
        
        # Check for exact matches and common patterns
        if text == 'localhost' or text == '127.0.0.1' or text == '0.0.0.0':
            return True
        
        # Check for ::1 but only as standalone or in URL context
        if (text == '::1' or 
            '//::1' in text or  # URLs like http://[::1]
            'localhost' in text):
            return True
        
        # Check for 127.x.x.x range using regex
        import re
        # Match 127.0-255.0-255.0-255 (IPv4 loopback range)
        ipv4_127_pattern = r'\b127\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'
        if re.search(ipv4_127_pattern, text):
            return True
        
        # Check for IPv6 loopback variations (must be exact match or standalone)
        ipv6_loopback_patterns = [
            r'(?:^|[\s:/])::1(?:[\s:/$]|$)',  # ::1 as standalone or in URL context
            r'\[::1\]',                        # IPv6 loopback in brackets (URLs)  
            r'(?:^|[\s:/])0:0:0:0:0:0:0:1(?:[\s:/$]|$)', # Full IPv6 loopback
        ]
        
        for pattern in ipv6_loopback_patterns:
            if re.search(pattern, text):
                return True
        
        return False
    
    # Registry Methods
    @classmethod
    def register_test(cls, test_class: Union[Type['TestTypeBase'], str], 
                     shortname: str, 
                     is_public: bool = True,
                     description: Optional[str] = None) -> None:
        """
        Register a test type class for discovery and invocation
        
        Args:
            test_class: The test class to register (can be class object or string name)
            shortname: Short identifier for the test type
            is_public: Whether this test should be externally exposed
            description: Optional human-readable description
        """
        # Handle both class objects and string names
        if isinstance(test_class, str):
            class_name = test_class
            # If description not provided, we'll need to resolve the class to get it
            if description is None:
                # We'll set a placeholder and resolve it later when needed
                description = f"Test type: {shortname}"
        else:
            class_name = test_class.__name__
            if description is None:
                # Get description from TestMetadata
                metadata = test_class.describe_self()
                description = metadata.description
        
        cls._test_registry[shortname] = TestTypeInfo(
            test_class_name=class_name,
            test_shortname=shortname,
            is_public=is_public,
            description=description
        )
    
    @classmethod
    def test_types(cls) -> Dict[str, TestTypeInfo]:
        """Return dictionary of all registered test types keyed by shortname"""
        return cls._test_registry.copy()
    
    @classmethod
    def public_test_types(cls) -> Dict[str, TestTypeInfo]:
        """Return dictionary of only publicly exposed test types"""
        return {shortname: info for shortname, info in cls._test_registry.items() 
                if info.is_public}
    
    @classmethod
    def load_test_plugin(cls, shortname: str) -> Type['TestTypeBase']:
        """
        Load a test plugin by shortname using the dynamic plugin loader.
        
        This method is primarily intended for testing purposes, allowing tests to load
        plugins using the same mechanism that the CLI uses at runtime. This avoids
        import path issues with relative imports in plugin modules.
        
        Args:
            shortname: The plugin shortname (e.g., 'gmail', 'http', 'ssh')
            
        Returns:
            The loaded test class (subclass of TestTypeBase)
            
        Raises:
            ValueError: If the plugin file doesn't exist or doesn't contain a valid test class
            
        Example:
            >>> TestTypeGmail = TestTypeBase.load_test_plugin('gmail')
            >>> test = TestTypeGmail({'user_email': 'test@example.com'})
            >>> result = test.run_test()
        """
        import importlib.util
        import sys
        from pathlib import Path
        
        # Find the test_plugins directory
        # This file is in conn_tester_support/core.py
        # test_plugins is in conn_tester_support/test_plugins/
        core_file = Path(__file__)
        conn_tester_support_dir = core_file.parent
        plugin_dir = conn_tester_support_dir / 'test_plugins'
        test_file = plugin_dir / f"conntest_{shortname}.py"
        
        if not test_file.exists():
            raise ValueError(f"Test plugin '{shortname}' not found (looked for {test_file})")
        
        # Temporarily add the parent directory (cli/) to sys.path so relative imports work
        # The plugins use "from conn_tester_support.core import ..." which requires
        # the cli/ directory to be in the path
        cli_dir = conn_tester_support_dir.parent
        path_added = False
        if str(cli_dir) not in sys.path:
            sys.path.insert(0, str(cli_dir))
            path_added = True
        
        try:
            # Dynamic import and class discovery
            spec = importlib.util.spec_from_file_location(f"conntest_{shortname}", test_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            # Find TestTypeBase subclass that's actually defined in this module
            # Store the module object in sys.modules so we can check against it
            import sys
            sys.modules[f"conntest_{shortname}"] = module
            
            for attr_name in dir(module):
                if not attr_name.startswith('TestType'):
                    continue
                if attr_name == 'TestTypeBase':  # Skip the base class itself
                    continue
                    
                attr = getattr(module, attr_name)
                if not isinstance(attr, type):
                    continue
                
                # Check if it has TestTypeBase in its MRO by name (not by identity)
                has_test_type_base = any(base.__name__ == 'TestTypeBase' for base in attr.__mro__)
                if not has_test_type_base:
                    continue
                
                # Check if this class's module name matches our loaded module name
                # Since we stored the module in sys.modules, classes defined in it
                # will have __module__ == f"conntest_{shortname}"
                if attr.__module__ == f"conntest_{shortname}":
                    return attr
            
            raise ValueError(f"No TestTypeBase subclass found in conntest_{shortname}.py")
        
        finally:
            # Clean up sys.path if we added to it
            if path_added and str(cli_dir) in sys.path:
                sys.path.remove(str(cli_dir))
    
    # Instance Methods
    @abstractmethod
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        """Execute the test and return detailed results"""
        pass
    
    def get_configs(self) -> Dict[str, Any]:
        """Return current configuration as dictionary"""
        return self.config.copy()
    
    def get_test_result(self) -> Optional[TestResult]:
        """Return the result of the last test run"""
        return self._last_result
    
    def get_prerequisite_results(self) -> Dict[str, Any]:
        """Return detailed results from prerequisite tests (DNS, TCP, etc.)"""
        if self._last_result:
            return self._last_result.extra_detail
        return {}
    
    @classmethod
    def create_env_var_error(cls, env_var_name: str, description: str = None, alternative_suggestion: str = None) -> TestResult:
        """
        Create a standardized error message for missing environment variables.
        
        This convenience method provides consistent error messaging across all test types
        that require environment variables for credentials or configuration.
        
        Args:
            env_var_name: Name of the required environment variable (e.g., 'SSH_PASSWORD')
            description: Optional description of what the variable is used for
            alternative_suggestion: Optional alternative solution (e.g., key-based auth)
            
        Returns:
            TestResult with standardized error message
            
        Example:
            # In a test class that requires API_KEY environment variable
            api_key = os.getenv('API_KEY')
            if not api_key:
                return self.create_env_var_error(
                    env_var_name='API_KEY',
                    description='API authentication',
                    alternative_suggestion='Or provide --api-token option for direct token authentication'
                )
        """
        message = f"{env_var_name} environment variable is required"
        if description:
            message += f" for {description}"
        
        advice = [
            f"Set {env_var_name} environment variable in your shell:",
            f"  export {env_var_name}='your_value_here'"
        ]
        
        if alternative_suggestion:
            advice.append(alternative_suggestion)
        
        return TestResult(
            success=False,
            error_type="config_invalid",
            error_message=message,
            advice=advice
        )


# ============================================================================
# Credential & Security Utilities
# ============================================================================

def _hash_credential(credential: str) -> str:
    """Hash a credential using SHA256 for change tracking with shortened display"""
    import base64
    
    # Create SHA256 hash
    hash_obj = hashlib.sha256(credential.encode())
    hash_bytes = hash_obj.digest()
    
    # Take first 8 bytes (64 bits) for compact display
    # This gives us 2^64 ≈ 18.4 × 10^18 possible values
    # 1% collision chance at ~2^28 ≈ 268 million passwords
    short_hash = base64.b64encode(hash_bytes[:8]).decode('ascii').rstrip('=')
    
    return f"**HASHED:{short_hash}**"


def _redact_sensitive_fields(config: Dict[str, Any]) -> Dict[str, Any]:
    """Identify and redact sensitive fields in configuration"""
    sensitive_patterns = [
        'password', 'passphrase', 'secret', 'client_secret', 'token', 
        'access_token', 'refresh_token', 'authorization', 'private_key'
    ]
    
    redacted = config.copy()
    for key, value in redacted.items():
        if isinstance(value, str) and any(pattern in key.lower() for pattern in sensitive_patterns):
            redacted[key] = "***REDACTED***"
        elif isinstance(value, dict):
            redacted[key] = _redact_sensitive_fields(value)
    
    return redacted


def _detect_credential_changes(old_config: Dict[str, Any], new_config: Dict[str, Any]) -> bool:
    """Detect if credentials have changed between configurations"""
    sensitive_patterns = [
        'password', 'passphrase', 'secret', 'client_secret', 'token', 
        'access_token', 'refresh_token', 'authorization', 'private_key'
    ]
    
    for key in new_config:
        if any(pattern in key.lower() for pattern in sensitive_patterns):
            old_val = old_config.get(key, "")
            new_val = new_config.get(key, "")
            if old_val != new_val:
                return True
    
    return False
