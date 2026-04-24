# Connection Test Plugins

This directory contains modular test plugins for the `conn_tester` command-line tool. Each plugin implements a specific type of connectivity test (SSH, HTTP, DNS, etc.) and can be used independently or as prerequisites for other tests.

## Overview

The connection testing system is built around a plugin architecture where each test type is implemented as a separate Python module. Tests can have prerequisite dependencies (e.g., SSH tests require DNS resolution and TCP connectivity first) and provide detailed error reporting with actionable advice.

## Plugin Architecture

### Core Components

- **`TestTypeBase`**: Abstract base class that all test plugins inherit from
- **`TestMetadata`**: Describes test configuration fields and requirements  
- **`TestResult`**: Standardized result format with success/failure, error details, and advice
- **`ConfigurationError`**: Exception for invalid test configuration

### Key Features

- **Prerequisite Testing**: Tests can depend on other tests (DNS → TCP → SSH)
- **Credential Security**: Automatic hashing and redaction of sensitive data
- **Detailed Error Reporting**: Specific error types with actionable advice
- **Environment Variable Support**: Secure credential handling via environment variables
- **Auto-Registration**: Tests automatically register themselves when imported

## Creating a New Test Plugin

### 1. Basic Structure

Create a new Python file in this directory following the naming pattern `conntest_<testname>.py`:

```python
#!/usr/bin/env python3
"""
<Test Type> connection test plugin.

This module provides <test type> connectivity testing functionality,
including prerequisite checks and detailed error reporting.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Type

from conn_tester_support.core import TestTypeBase
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve


class TestTypeYourTest(TestTypeBase):
    """<Test type> connectivity and authentication testing"""
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Tests <test type> connectivity and authentication",
            config_fields={
                "hostname": "Target hostname or IP address",
                "port": "Target port number (default: <default_port>)",
                "timeout_s": "Connection timeout in seconds",
                # Add your specific fields here
            },
            required_fields=["hostname"],
            optional_fields=["port", "timeout_s"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve]  # Add other prerequisites as needed
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        # Implementation here
        pass


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeYourTest,
    shortname="your-test",
    is_public=True,
    description="Tests <test type> connectivity and authentication"
)
```

### 2. Configuration Validation

Always validate required configuration parameters and raise `ConfigurationError` for invalid inputs:

```python
def run_test(self, logger: Optional[Any] = None) -> TestResult:
    # Validate configuration
    if not self.config.get("hostname"):
        raise ConfigurationError("hostname is required")
    
    hostname = self.config.get("hostname")
    port = self.config.get("port") or DEFAULT_PORT
    timeout_s = self.config.get("timeout_s") or 30
    
    # Validate port number
    try:
        port = int(port)
        if not (1 <= port <= 65535):
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid port number: {port}",
                advice=["Port must be between 1 and 65535"]
            )
    except (ValueError, TypeError):
        return TestResult(
            success=False,
            error_type="config_invalid", 
            error_message=f"Invalid port number: {port}",
            advice=["Port must be a valid integer"]
        )
```

### 3. Prerequisite Testing

Run prerequisite tests first and include their results in your test output:

```python
def run_test(self, logger: Optional[Any] = None) -> TestResult:
    # ... configuration validation ...
    
    # Run prerequisite tests
    prerequisite_results = {}
    
    # DNS resolution
    dns_test = TestTypeDnsResolve({"hostname": hostname})
    dns_result = dns_test.run_test()
    prerequisite_results.update(dns_result.extra_detail)
    
    if not dns_result.success:
        return TestResult(
            success=False,
            error_type="dns_resolve",
            error_message="DNS resolution failed",
            advice=dns_result.advice,
            extra_detail=prerequisite_results
        )
    
    # TCP connectivity (if needed)
    tcp_test = TestTypeTcpConnect({"host": hostname, "port": port})
    tcp_result = tcp_test.run_test()
    prerequisite_results.update(tcp_result.extra_detail)
    
    if not tcp_result.success:
        return TestResult(
            success=False,
            error_type="tcp_connect",
            error_message="TCP connectivity failed", 
            advice=tcp_result.advice,
            extra_detail=prerequisite_results
        )
```

### 4. Credential Handling

For tests requiring credentials, use environment variables with proper error handling:

```python
def run_test(self, logger: Optional[Any] = None) -> TestResult:
    # ... configuration validation ...
    
    # Handle authentication
    if private_key_filepath:
        # Key-based authentication
        if not os.path.exists(private_key_filepath):
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Private key file not found: {private_key_filepath}",
                advice=["Provide a valid path to the private key file"]
            )
        auth_method = "key_file"
    else:
        # Password-based authentication
        password = os.getenv('YOUR_PASSWORD_VAR')
        if not password:
            return self.create_env_var_error(
                env_var_name='YOUR_PASSWORD_VAR',
                description='password-based authentication',
                alternative_suggestion='Or provide --private-key-filepath option for key-based authentication'
            )
        auth_method = "password"
```

### 5. Detailed Error Handling

Provide specific error types and actionable advice for different failure scenarios:

```python
try:
    # Your connection logic here
    pass
except SpecificException as e:
    error_msg = str(e)
    
    if "Connection refused" in error_msg:
        error_type = "connect_refused"
        advice = [
            "Check if the service is running on the target server",
            "Verify the port number is correct",
            "Check firewall settings on both client and server"
        ]
    elif "Connection timed out" in error_msg:
        error_type = "timeout"
        advice = [
            "Check network connectivity to the target host",
            "Verify the hostname/IP address is correct",
            "Try increasing the timeout value"
        ]
    elif "Permission denied" in error_msg:
        error_type = "auth_failed"
        advice = [
            "Check username and credentials",
            "Verify the user account exists on the server",
            "Check if the user has access permissions"
        ]
    else:
        error_type = "connect_failed"
        advice = [
            "Check service configuration on the target server",
            "Verify credentials and authentication method",
            "Check network connectivity and firewall settings"
        ]
    
    return TestResult(
        success=False,
        error_type=error_type,
        error_message=f"Connection error: {error_msg}",
        advice=advice,
        extra_detail={
            **prerequisite_results,
            "connection_details": {
                "error": str(e),
                "exception_type": type(e).__name__
            }
        }
    )
```

### 6. Success Results

Include comprehensive details in successful test results:

```python
# After successful connection
return TestResult(
    success=True,
    extra_detail={
        **prerequisite_results,
        "connection_details": {
            "ok": True,
            "hostname": hostname,
            "port": port,
            "connection_time_ms": round(connection_time_ms, 2),
            "auth_method": auth_method,
            "response_data": output  # Any relevant response data
        }
    }
)
```

### 7. Configuration Display

Implement `get_configs()` to return configuration with credential hashing:

```python
def get_configs(self) -> Dict[str, Any]:
    """Return current configuration as dictionary with credential hashing and defaults applied"""
    config = self.config.copy()
    
    # Apply defaults for parameters that weren't explicitly set
    if config.get("port") is None:
        config["port"] = DEFAULT_PORT
    if config.get("timeout_s") is None:
        config["timeout_s"] = 30
    
    # Hash password if present in environment
    if 'YOUR_PASSWORD_VAR' in os.environ and not config.get('private_key_filepath'):
        password = os.environ['YOUR_PASSWORD_VAR']
        config['password'] = _hash_credential(password)
    
    return config
```

## Available Prerequisite Tests

- **`TestTypeDnsResolve`**: DNS resolution testing
- **`TestTypeSshResponds`**: TCP connectivity to SSH port 22
- **`TestTypeHttpsResponds`**: TCP connectivity to HTTPS port 443
- **`TestTypeHttpResponds`**: TCP connectivity to HTTP port 80

## Error Types

Use these standardized error types for consistency:

- `config_invalid`: Configuration parameter errors
- `dns_resolve`: DNS resolution failures
- `tcp_connect`: TCP connectivity failures
- `timeout`: Connection timeouts
- `auth_failed`: Authentication failures
- `connect_refused`: Connection refused errors
- `network_error`: General network errors

## Best Practices

1. **Always validate configuration** before attempting connections
2. **Run prerequisite tests** and include their results in your output
3. **Provide specific error types** with actionable advice
4. **Use environment variables** for credentials, never hardcode them
5. **Include timing information** in successful results
6. **Hash sensitive data** in configuration display
7. **Handle all exception types** with appropriate error messages
8. **Include connection details** in both success and failure results
9. **Use descriptive test descriptions** and field descriptions
10. **Register tests with appropriate shortnames** and public visibility

## Example: Complete SSH Test

See `conntest_ssh.py` for a comprehensive example that demonstrates:

- Prerequisite testing (DNS → TCP → SSH)
- Multiple authentication methods (password vs key-based)
- Detailed error handling for different SSH failure scenarios
- Credential security with environment variables
- Comprehensive result reporting

## Testing Your Plugin

After creating your plugin:

1. Import it in the main `conn_tester.py` to ensure auto-registration
2. Test with: `python conn_tester.py list` (should show your test)
3. Test execution: `python conn_tester.py your-test --param value --file logfile.yaml`
4. Verify error handling with invalid configurations
5. Check that prerequisite tests run automatically

## Integration

Your test will automatically be available as a subcommand:

```bash
python conn_tester.py your-test --hostname example.com --port 1234 --file results.yaml
```

The system handles argument parsing, configuration validation, and result logging based on your `describe_self()` metadata.

## Gotchas and Key Tips

Here are the 13 lessons generalized for any conn_tester plugin:

### Critical Design Decisions (Would Save Major Rework):

1. **Never use file paths as parameters** - Server environments can't rely on filesystem access. Break credentials into granular components (IDs, emails, URLs) and pass sensitive parts via environment variables.

2. **Always show literal API/system errors first in advice** - Start with the exact error message from the remote system, then add interpretation. Users need verbatim errors to research edge cases your advice doesn't cover.

3. **Test with real credentials/systems early** - Mock tests miss real-world issues like encoding problems, timeout behavior, rate limiting, and unexpected API responses. Run actual connections as soon as basic logic works.

4. **Empty/null result sets should fail the test** - A successful API call that returns zero data doesn't verify the connection works. You must actually retrieve something to confirm full access.

5. **Validate parameter necessity by testing with wrong values** - Don't assume parameters are required/validated. Test with incorrect values to confirm they're actually used and checked by the remote system.

### Architecture & Testing:

6. **Use relative imports in plugins** - Follow the pattern: `from conn_tester_support.core import TestTypeBase`. Don't use absolute imports like `from totodev_pub.cli.conn_tester_support...`

7. **Test plugins using TestTypeBase.load_test_plugin()** - Don't directly import plugin modules. Use the dynamic loader to avoid import path issues with relative imports.

8. **Study existing plugins before designing** - Check similar plugins (MS365, SharePoint, SSH) for established patterns in error handling, optional tests, and parameter structure.

9. **Test all invocation methods** - Verify: `python -m totodev_pub.cli.conn_tester`, `python conn_tester.py`, stdout (`-f -`), logfile, and parameter persistence from previous runs.

### Error Handling & User Experience:

10. **Provide concrete alternatives in advice** - Instead of "try a different value," list specific options: "Try: INBOX, SENT, DRAFTS" or "Common ports: 22 (SSH), 443 (HTTPS), 3389 (RDP)"

11. **Add contingent tests for ambiguous failures** - When something fails, test alternative explanations. Empty results might mean wrong identifier, misspelled name, or genuinely empty.

12. **Security violations must fail the overall test** - If optional "forbidden resource" test succeeds (excessive permissions), immediately return failure - don't continue to success.

13. **Structure advice consistently: Error → Interpretation → Steps** - Format: "API error: {literal_message}" → blank line → "This usually means..." → "Troubleshooting steps: 1. 2. 3."

