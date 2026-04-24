#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
DNS resolution connection test plugin.

This module provides DNS resolution testing functionality for validating
hostname resolution before attempting other connection tests.
"""

from __future__ import annotations

import socket
import time
from typing import Any, Optional

from conn_tester_support.core import TestTypeBase, DEFAULT_DNS_TIMEOUT
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError


class TestTypeDnsResolve(TestTypeBase):
    """DNS resolution test for hostname validation"""
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Validates DNS resolution for hostnames",
            config_fields={
                "hostname": "Target hostname to resolve",
                "timeout_s": "DNS resolution timeout in seconds"
            },
            required_fields=["hostname"],
            optional_fields=["timeout_s"]
        )
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        # Validate configuration
        if not self.config.get("hostname"):
            raise ConfigurationError("hostname is required")
        
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
        timeout_s = self.config.get("timeout_s", DEFAULT_DNS_TIMEOUT)
        
        try:
            start_time = time.time()
            result = socket.getaddrinfo(hostname, None, socket.AF_INET)
            end_time = time.time()
            
            addresses = [addr[4][0] for addr in result]
            rtt_ms = (end_time - start_time) * 1000
            
            return TestResult(
                success=True,
                extra_detail={
                    "dns_resolve": {
                        "ok": True,
                        "rtt_ms": round(rtt_ms, 2)
                    }
                }
            )
        except socket.gaierror as e:
            return TestResult(
                success=False,
                error_type="dns_resolve",
                error_message=f"DNS resolution failed: {e}",
                advice=["Check hostname spelling", "If DNS change is very recent, it can take up to 30 minutes to be visible."],
                extra_detail={
                    "dns_resolve": {
                        "ok": False,
                        "error": str(e)
                    }
                }
            )
        except Exception as e:
            return TestResult(
                success=False,
                error_type="network_error",
                error_message=f"Unexpected error during DNS resolution: {e}",
                advice=["Check network connectivity", "Try again later"],
                extra_detail={
                    "dns_resolve": {
                        "ok": False,
                        "error": str(e)
                    }
                }
            )


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeDnsResolve,
    shortname="dns",
    is_public=False,
    description="DNS resolution for hostname validation"
)
