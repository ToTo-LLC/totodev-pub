#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
HTTP TCP connectivity test plugin.

This module provides TCP connectivity testing for HTTP port 80,
used as a prerequisite for HTTP connection tests.
"""

from __future__ import annotations

import socket
import time
from typing import Any, Optional

from conn_tester_support.core import TestTypeBase, DEFAULT_TCP_TIMEOUT, HTTP_PORT
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError


class TestTypeHttpResponds(TestTypeBase):
    """TCP connection test for HTTP port 80"""
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Tests TCP connectivity to HTTP port 80",
            config_fields={
                "host": "Target hostname or IP address",
                "port": "Target port number (default: 80)",
                "timeout_s": "Connection timeout in seconds"
            },
            required_fields=["host"],
            optional_fields=["port", "timeout_s"]
        )
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        # Validate configuration
        if not self.config.get("host"):
            raise ConfigurationError("host is required")
        
        host = self.config.get("host")
        
        # Check for loopback addresses
        if self.contains_loopback(host):
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
        port = self.config.get("port", HTTP_PORT)
        timeout_s = self.config.get("timeout_s", DEFAULT_TCP_TIMEOUT)
        
        try:
            start_time = time.time()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            result = sock.connect_ex((host, port))
            end_time = time.time()
            sock.close()
            
            rtt_ms = (end_time - start_time) * 1000
            
            if result == 0:
                return TestResult(
                    success=True,
                    extra_detail={
                        "tcp_connect": {
                            "ok": True,
                            "remote_ip": host,
                            "port": port,
                            "rtt_ms": round(rtt_ms, 2)
                        }
                    }
                )
            else:
                return TestResult(
                    success=False,
                    error_type="tcp_connect",
                    error_message=f"TCP connection to {host}:{port} failed",
                    advice=["Check if service is running", "Verify firewall settings"],
                    extra_detail={
                        "tcp_connect": {
                            "ok": False,
                            "remote_ip": host,
                            "port": port,
                            "error_code": result
                        }
                    }
                )
        except socket.timeout:
            return TestResult(
                success=False,
                error_type="timeout",
                error_message=f"TCP connection to {host}:{port} timed out",
                advice=["Check network connectivity", "Increase timeout value"],
                extra_detail={
                    "tcp_connect": {
                        "ok": False,
                        "remote_ip": host,
                        "port": port,
                        "error": "timeout"
                    }
                }
            )
        except Exception as e:
            return TestResult(
                success=False,
                error_type="network_error",
                error_message=f"Unexpected error during TCP connection: {e}",
                advice=["Check network connectivity", "Verify hostname/IP"],
                extra_detail={
                    "tcp_connect": {
                        "ok": False,
                        "remote_ip": host,
                        "port": port,
                        "error": str(e)
                    }
                }
            )


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeHttpResponds,
    shortname="http-responds",
    is_public=False,
    description="Tests TCP connectivity to HTTP port 80"
)
