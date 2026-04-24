#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
DNS mapping validation connection test plugin.

This module provides DNS mapping validation functionality to confirm that
a specific domain name resolves to an expected IP address.
"""

from __future__ import annotations

import socket
import time
from typing import Any, Dict, List, Optional, Type

from conn_tester_support.core import TestTypeBase, DEFAULT_DNS_TIMEOUT
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve


class TestTypeDnsMap(TestTypeBase):
    """DNS mapping validation test to confirm domain resolves to expected IP"""
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Validates that a domain name resolves to a specific expected IP address",
            config_fields={
                "hostname": "Target hostname to resolve",
                "expected_ip": "Expected IP address that the hostname should resolve to",
                "timeout_s": "DNS resolution timeout in seconds"
            },
            required_fields=["hostname", "expected_ip"],
            optional_fields=["timeout_s"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve]
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        # Validate configuration
        hostname = self.config.get("hostname")
        expected_ip = self.config.get("expected_ip")
        timeout_s = self.config.get("timeout_s", DEFAULT_DNS_TIMEOUT)
        
        if not hostname:
            raise ConfigurationError("hostname is required")
        if not expected_ip:
            raise ConfigurationError("expected_ip is required")
        
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
        
        if self.contains_loopback(expected_ip):
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
        
        # Validate that expected_ip is a valid IP address format
        try:
            socket.inet_aton(expected_ip)
        except socket.error:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid IP address format: {expected_ip}",
                advice=["Provide a valid IPv4 address (e.g., '192.168.1.1')"]
            )
        
        # Run prerequisite DNS test first
        prerequisite_results = {}
        
        dns_test = TestTypeDnsResolve({"hostname": hostname, "timeout_s": timeout_s})
        dns_result = dns_test.run_test()
        prerequisite_results.update(dns_result.extra_detail)
        
        if not dns_result.success:
            return TestResult(
                success=False,
                error_type="dns_resolve",
                error_message=f"DNS resolution failed for {hostname}",
                advice=dns_result.advice,
                extra_detail=prerequisite_results
            )
        
        # Now perform the specific IP mapping validation
        try:
            start_time = time.time()
            result = socket.getaddrinfo(hostname, None, socket.AF_INET)
            end_time = time.time()
            
            # Extract all resolved IP addresses and remove duplicates while preserving order
            resolved_ips_raw = [addr[4][0] for addr in result]
            resolved_ips = list(dict.fromkeys(resolved_ips_raw))  # Remove duplicates while preserving order
            rtt_ms = (end_time - start_time) * 1000
            
            # Check if expected IP is in the resolved addresses
            if expected_ip in resolved_ips:
                return TestResult(
                    success=True,
                    extra_detail={
                        **prerequisite_results,
                        "dns_mapping": {
                            "ok": True,
                            "hostname": hostname,
                            "expected_ip": expected_ip,
                            "resolved_ips": resolved_ips,
                            "rtt_ms": round(rtt_ms, 2),
                            "mapping_confirmed": True
                        }
                    }
                )
            else:
                # DNS resolves but to different IP(s)
                return TestResult(
                    success=False,
                    error_type="dns_mapping_mismatch",
                    error_message=f"DNS mapping mismatch: {hostname} resolves to {resolved_ips} but expected {expected_ip}",
                    advice=[
                        f"Expected {hostname} to resolve to {expected_ip}",
                        f"Actually resolves to: {', '.join(resolved_ips)}",
                        "Check DNS configuration",
                        "If DNS change is recent, it can take up to 30 minutes to propagate"
                    ],
                    extra_detail={
                        **prerequisite_results,
                        "dns_mapping": {
                            "ok": False,
                            "hostname": hostname,
                            "expected_ip": expected_ip,
                            "resolved_ips": resolved_ips,
                            "rtt_ms": round(rtt_ms, 2),
                            "mapping_confirmed": False,
                            "mismatch_details": {
                                "expected": expected_ip,
                                "actual": resolved_ips
                            }
                        }
                    }
                )
                
        except socket.gaierror as e:
            return TestResult(
                success=False,
                error_type="dns_resolve",
                error_message=f"DNS resolution failed: {e}",
                advice=[
                    "Check hostname spelling",
                    "If DNS change is very recent, it can take up to 30 minutes to be visible."
                ],
                extra_detail={
                    **prerequisite_results,
                    "dns_mapping": {
                        "ok": False,
                        "hostname": hostname,
                        "expected_ip": expected_ip,
                        "error": str(e)
                    }
                }
            )
        except Exception as e:
            return TestResult(
                success=False,
                error_type="network_error",
                error_message=f"Unexpected error during DNS mapping validation: {e}",
                advice=["Check network connectivity", "Try again later"],
                extra_detail={
                    **prerequisite_results,
                    "dns_mapping": {
                        "ok": False,
                        "hostname": hostname,
                        "expected_ip": expected_ip,
                        "error": str(e)
                    }
                }
            )


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeDnsMap,
    shortname="dns_map",
    is_public=True,
    description="Validates that a domain name resolves to a specific expected IP address"
)
