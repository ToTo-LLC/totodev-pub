#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
SSH connection test plugin.

This module provides SSH connectivity and authentication testing functionality,
including prerequisite DNS and TCP connectivity checks.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Any, Dict, List, Optional, Type

from totodev_pub.optional_dependencies import build_missing_dependency_message
from conn_tester_support.core import TestTypeBase, SSH_PORT, _hash_credential
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve
from conn_tester_support.test_plugins.conntest_ssh_responds import TestTypeSshResponds


class TestTypeSsh(TestTypeBase):
    """SSH connectivity and authentication testing"""
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Tests SSH connectivity and authentication to remote servers",
            config_fields={
                "user": "SSH username for authentication",
                "hostname": "Target hostname or IP address",
                "port": "SSH port number (default: 22)",
                "private_key_filepath": "Path to private key file for key-based authentication",
                "timeout_s": "Connection timeout in seconds"
            },
            required_fields=["user", "hostname"],
            optional_fields=["port", "private_key_filepath", "timeout_s"],
            confidential_fields=["SSH_PASSWORD"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve, TestTypeSshResponds]
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        # Validate configuration
        if not self.config.get("user"):
            raise ConfigurationError("user is required")
        if not self.config.get("hostname"):
            raise ConfigurationError("hostname is required")
        
        user = self.config.get("user")
        hostname = self.config.get("hostname")
        
        # Check for loopback addresses
        if self.contains_loopback(hostname):
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message="Loopback addresses are not allowed for security reasons",
                advice=[
                    "Loopback addresses (localhost, 127.x.x.x, ::1, 0.0.0.0) are blocked",
                    "Use a remote hostname or IP address instead",
                    "This prevents accidental testing of local services"
                ]
            )
        port = self.config.get("port") or SSH_PORT
        timeout_s = self.config.get("timeout_s") or 30
        private_key_filepath = self.config.get("private_key_filepath")
        
        # Validate port
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
        
        # Validate authentication method
        if private_key_filepath:
            # Key-based authentication
            if not os.path.exists(private_key_filepath):
                return TestResult(
                    success=False,
                    error_type="config_invalid",
                    error_message=f"Private key file not found: {private_key_filepath}",
                    advice=["Provide a valid path to the private key file"]
                )
            if not os.access(private_key_filepath, os.R_OK):
                return TestResult(
                    success=False,
                    error_type="config_invalid",
                    error_message=f"Private key file not readable: {private_key_filepath}",
                    advice=["Ensure the private key file is readable"]
                )
            auth_method = "key_file"
        else:
            # Password-based authentication
            password = os.getenv('SSH_PASSWORD')
            if not password:
                return self.create_env_var_error(
                    env_var_name='SSH_PASSWORD',
                    description='password-based authentication',
                    alternative_suggestion='Or provide --private-key-filepath option for key-based authentication'
                )
            auth_method = "password"
        
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
        
        # SSH port connectivity
        ssh_test = TestTypeSshResponds({"host": hostname, "port": port})
        ssh_result = ssh_test.run_test()
        prerequisite_results.update(ssh_result.extra_detail)
        
        if not ssh_result.success:
            return TestResult(
                success=False,
                error_type="tcp_connect",
                error_message="SSH port connectivity failed",
                advice=ssh_result.advice,
                extra_detail=prerequisite_results
            )
        
        # Perform actual SSH connection test using paramiko
        try:
            import paramiko
        except ImportError:
            install_hint = build_missing_dependency_message(
                feature="SSH testing",
                packages=["paramiko"],
                extra="connectors",
            )
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=install_hint,
                advice=[install_hint, "This library provides cross-platform SSH functionality"],
                extra_detail={
                    **prerequisite_results,
                    "ssh_connection": {
                        "ok": False,
                        "hostname": hostname,
                        "port": port,
                        "user": user,
                        "auth_method": auth_method,
                        "error": "paramiko not available"
                    }
                }
            )
        
        try:
            start_time = time.time()
            
            # Create SSH client
            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # Attempt SSH connection
            if auth_method == "key_file":
                # Key-based authentication
                ssh_client.connect(
                    hostname=hostname,
                    port=port,
                    username=user,
                    key_filename=private_key_filepath,
                    timeout=timeout_s,
                    look_for_keys=False,  # Don't look for keys in default locations
                    allow_agent=False     # Don't use SSH agent
                )
            else:
                # Password-based authentication
                ssh_client.connect(
                    hostname=hostname,
                    port=port,
                    username=user,
                    password=password,
                    timeout=timeout_s,
                    look_for_keys=False,  # Don't look for keys in default locations
                    allow_agent=False     # Don't use SSH agent
                )
            
            # Test connection by executing a simple command
            stdin, stdout, stderr = ssh_client.exec_command('echo "SSH connection successful"')
            output = stdout.read().decode('utf-8').strip()
            error_output = stderr.read().decode('utf-8').strip()
            
            # Close connection
            ssh_client.close()
            
            end_time = time.time()
            connection_time_ms = (end_time - start_time) * 1000
            
            return TestResult(
                success=True,
                extra_detail={
                    **prerequisite_results,
                    "ssh_connection": {
                        "ok": True,
                        "hostname": hostname,
                        "port": port,
                        "user": user,
                        "auth_method": auth_method,
                        "connection_time_ms": round(connection_time_ms, 2),
                        "ssh_output": output
                    }
                }
            )
            
        except paramiko.AuthenticationException as e:
            # Get more detailed error information
            error_details = {
                "error_type": "authentication_failed",
                "error_message": str(e),
                "auth_method": auth_method
            }
            
            # Provide specific advice based on authentication method
            if auth_method == "password":
                advice = [
                    "Check username and password combination",
                    "Verify the user account exists on the server",
                    "Check if password authentication is enabled on the server",
                    "Ensure the user account is not locked or disabled"
                ]
                error_details["auth_details"] = {
                    "method": "password",
                    "note": "Password authentication failed"
                }
            else:
                advice = [
                    "Check if the private key file exists and is readable",
                    "Verify the private key file format (should be OpenSSH or PEM format)",
                    "Check if the public key is installed in ~/.ssh/authorized_keys on the server",
                    "Verify the key file permissions (should be 600 for private key)",
                    "Check if key-based authentication is enabled on the server"
                ]
                error_details["auth_details"] = {
                    "method": "key_file",
                    "key_file": private_key_filepath,
                    "note": "Key-based authentication failed"
                }
            
            return TestResult(
                success=False,
                error_type="ssh_auth_failed",
                error_message=f"SSH authentication failed: {str(e)}",
                advice=advice,
                extra_detail={
                    **prerequisite_results,
                    "ssh_connection": error_details
                }
            )
        except paramiko.SSHException as e:
            # Analyze the SSH exception for more specific error information
            error_msg = str(e)
            error_details = {
                "error_message": error_msg,
                "exception_type": type(e).__name__
            }
            
            # Provide specific advice based on the error message
            if "Connection refused" in error_msg or "Connection reset" in error_msg:
                error_type = "ssh_connect_refused"
                advice = [
                    "Check if SSH service is running on the target server",
                    "Verify the port number is correct (default is 22)",
                    "Check firewall settings on both client and server",
                    "Ensure the SSH service is listening on the specified port"
                ]
            elif "Connection timed out" in error_msg or "timeout" in error_msg.lower():
                error_type = "ssh_timeout"
                advice = [
                    "Check network connectivity to the target host",
                    "Verify the hostname/IP address is correct",
                    "Check if there are network routing issues",
                    "Try increasing the timeout value",
                    "Check if the target server is behind a firewall or NAT"
                ]
            elif "No route to host" in error_msg:
                error_type = "ssh_no_route"
                advice = [
                    "Check network connectivity and routing",
                    "Verify the hostname/IP address is correct",
                    "Check if the target server is reachable from your network",
                    "Try pinging the host to verify basic connectivity"
                ]
            elif "Permission denied" in error_msg:
                error_type = "ssh_permission_denied"
                advice = [
                    "Check username and credentials",
                    "Verify the user account exists on the server",
                    "Check if the user has SSH access permissions",
                    "Verify SSH service configuration allows the user to connect"
                ]
            else:
                error_type = "ssh_connect_failed"
                advice = [
                    "Check SSH service configuration on the target server",
                    "Verify credentials and authentication method",
                    "Check network connectivity and firewall settings",
                    "Review SSH server logs for additional error details"
                ]
            
            return TestResult(
                success=False,
                error_type=error_type,
                error_message=f"SSH connection error: {error_msg}",
                advice=advice,
                extra_detail={
                    **prerequisite_results,
                    "ssh_connection": error_details
                }
            )
        except socket.timeout:
            return TestResult(
                success=False,
                error_type="ssh_timeout",
                error_message=f"SSH connection timed out after {timeout_s} seconds",
                advice=[
                    "Check network connectivity to the target host",
                    "Verify the hostname/IP address is correct and reachable",
                    "Check if there are network routing issues or high latency",
                    "Try increasing the timeout value with --timeout-s option",
                    "Check if the target server is behind a firewall or NAT",
                    "Verify the SSH service is running and accessible on the specified port"
                ],
                extra_detail={
                    **prerequisite_results,
                    "ssh_connection": {
                        "error": "socket timeout",
                        "timeout_seconds": timeout_s,
                        "error_details": "Connection attempt exceeded the specified timeout period"
                    }
                }
            )
        except Exception as e:
            # Capture detailed error information for unexpected exceptions
            error_details = {
                "error": str(e),
                "exception_type": type(e).__name__,
                "error_details": "Unexpected error during SSH connection attempt"
            }
            
            # Provide more specific advice based on the exception type
            if isinstance(e, OSError):
                advice = [
                    "Check if the target host is reachable",
                    "Verify network connectivity and DNS resolution",
                    "Check if there are firewall or network restrictions",
                    "Ensure the SSH service is running on the target server"
                ]
                error_details["error_category"] = "network_os_error"
            elif isinstance(e, ConnectionError):
                advice = [
                    "Check network connectivity to the target host",
                    "Verify the hostname/IP address and port number",
                    "Check firewall settings on both client and server",
                    "Ensure the SSH service is listening on the specified port"
                ]
                error_details["error_category"] = "connection_error"
            else:
                advice = [
                    "Check SSH service configuration on the target server",
                    "Verify credentials and authentication method",
                    "Check network connectivity and firewall settings",
                    "Review SSH server logs for additional error details",
                    "Try with different authentication method if available"
                ]
                error_details["error_category"] = "general_error"
            
            return TestResult(
                success=False,
                error_type="ssh_connect_failed",
                error_message=f"Unexpected SSH connection error: {e}",
                advice=advice,
                extra_detail={
                    **prerequisite_results,
                    "ssh_connection": error_details
                }
            )
    
    def get_configs(self) -> Dict[str, Any]:
        """Return current configuration as dictionary with credential hashing and defaults applied"""
        config = self.config.copy()
        
        # Apply defaults for parameters that weren't explicitly set
        if config.get("port") is None:
            config["port"] = SSH_PORT
        if config.get("timeout_s") is None:
            config["timeout_s"] = 30
        
        # Hash password if present in environment
        if 'SSH_PASSWORD' in os.environ and not config.get('private_key_filepath'):
            password = os.environ['SSH_PASSWORD']
            config['password'] = _hash_credential(password)
        
        return config


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeSsh,
    shortname="ssh",
    is_public=True,
    description="Tests SSH connectivity and authentication to remote servers"
)
