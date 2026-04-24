#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Multi-port connectivity test plugin.

This module provides multi-port connectivity testing to identify firewall
blocking patterns by testing multiple ports simultaneously.
"""

from __future__ import annotations

import socket
import time
from typing import Any, Dict, List, Optional, Type

from conn_tester_support.core import TestTypeBase, DEFAULT_TCP_TIMEOUT
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve


class TestTypePortScan(TestTypeBase):
    """Multi-port connectivity test to identify firewall blocking patterns"""
    
    # Common ports that are frequently blocked by firewalls
    DEFAULT_PORTS = [22, 80, 443, 3389, 1433, 3306, 5432, 8080, 8443, 25, 587, 993, 995]
    
    # Port name mapping for verbose output
    PORT_NAMES = {
        22: "SSH", 80: "HTTP", 443: "HTTPS", 3389: "RDP", 
        1433: "SQL Server", 3306: "MySQL", 5432: "PostgreSQL",
        8080: "Alt HTTP", 8443: "Alt HTTPS", 25: "SMTP", 
        587: "SMTP Submission", 993: "IMAPS", 995: "POP3S"
    }
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Tests connectivity to multiple ports to identify firewall blocking patterns",
            config_fields={
                "host": "Target hostname or IP address",
                "ports": "Comma-separated list of ports to test (e.g., '22,80,443,3389'). If not provided, tests common ports: 22,80,443,3389,1433,3306,5432,8080,8443,25,587,993,995",
                "timeout_s": "Connection timeout per port in seconds (default: 3)"
            },
            required_fields=["host"],
            optional_fields=["ports", "timeout_s"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve]
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        # Validate configuration
        host = self.config.get("host")
        ports_str = self.config.get("ports")
        timeout_s = self.config.get("timeout_s") or 3  # Default 3 seconds per port if None or not provided
        
        if not host:
            raise ConfigurationError("host is required")
        
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
        
        # Parse ports
        if ports_str:
            try:
                ports = [int(p.strip()) for p in ports_str.split(',')]
                # Validate port numbers
                for port in ports:
                    if not (1 <= port <= 65535):
                        return TestResult(
                            success=False,
                            error_type="config_invalid",
                            error_message=f"Invalid port number: {port}",
                            advice=["Port numbers must be between 1 and 65535"]
                        )
            except ValueError as e:
                return TestResult(
                    success=False,
                    error_type="config_invalid",
                    error_message=f"Invalid ports format: {ports_str}",
                    advice=["Provide comma-separated port numbers (e.g., '22,80,443')"]
                )
        else:
            ports = self.DEFAULT_PORTS
        
        # Validate timeout
        try:
            timeout_s = float(timeout_s)
            if timeout_s <= 0:
                return TestResult(
                    success=False,
                    error_type="config_invalid",
                    error_message=f"Invalid timeout value: {timeout_s}",
                    advice=["Timeout must be a positive number"]
                )
        except (ValueError, TypeError):
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid timeout value: {timeout_s}",
                advice=["Timeout must be a valid number"]
            )
        
        # Run prerequisite DNS test
        prerequisite_results = {}
        
        dns_test = TestTypeDnsResolve({"hostname": host})
        dns_result = dns_test.run_test()
        prerequisite_results.update(dns_result.extra_detail)
        
        if not dns_result.success:
            return TestResult(
                success=False,
                error_type="dns_resolve",
                error_message=f"DNS resolution failed for {host}",
                advice=dns_result.advice,
                extra_detail=prerequisite_results
            )
        
        # Test each port
        port_results = {}
        open_ports = []
        closed_ports = []
        failed_ports = []
        total_time = 0
        
        if logger:
            logger.info(f"Starting port scan of {host} - testing {len(ports)} ports with {timeout_s}s timeout each")
        
        for i, port in enumerate(ports, 1):
            port_name = self.PORT_NAMES.get(port, f"Port {port}")
            if logger:
                logger.info(f"[{i}/{len(ports)}] Testing port {port} ({port_name}) with {timeout_s}s timeout...")
            try:
                start_time = time.time()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout_s)
                result = sock.connect_ex((host, port))
                end_time = time.time()
                sock.close()
                
                rtt_ms = (end_time - start_time) * 1000
                total_time += rtt_ms
                
                if result == 0:
                    # Port is open
                    open_ports.append(port)
                    port_results[str(port)] = {
                        "status": "open",
                        "rtt_ms": round(rtt_ms, 2),
                        "error_code": None
                    }
                    if logger:
                        logger.info(f"  ✓ Port {port} ({port_name}) OPEN - connected in {round(rtt_ms, 1)}ms")
                else:
                    # Port is closed/filtered
                    closed_ports.append(port)
                    port_results[str(port)] = {
                        "status": "closed",
                        "rtt_ms": round(rtt_ms, 2),
                        "error_code": result
                    }
                    if logger:
                        logger.info(f"  ✗ Port {port} ({port_name}) CLOSED - connection refused (error {result})")
                    
            except socket.timeout:
                # Timeout - likely firewall dropping packets
                failed_ports.append(port)
                port_results[str(port)] = {
                    "status": "timeout",
                    "rtt_ms": timeout_s * 1000,
                    "error": "Connection timeout"
                }
                total_time += timeout_s * 1000
                if logger:
                    logger.info(f"  ⏱ Port {port} ({port_name}) TIMEOUT - likely firewall blocked after {timeout_s}s")
                
            except Exception as e:
                # Other network error
                failed_ports.append(port)
                port_results[str(port)] = {
                    "status": "error",
                    "rtt_ms": None,
                    "error": str(e)
                }
                if logger:
                    logger.info(f"  ✗ Port {port} ({port_name}) ERROR - {str(e)}")
        
        # Determine overall success
        all_ports_tested = len(ports)
        successful_ports = len(open_ports)
        
        # Log scan summary
        if logger:
            logger.info(f"Port scan completed: {len(open_ports)} open, {len(closed_ports)} closed, {len(failed_ports)} failed/timeout")
            logger.info(f"Total scan time: {round(total_time/1000, 1)}s")
        
        # Success if all ports are reachable
        success = len(failed_ports) == 0 and len(closed_ports) == 0
        
        # Create detailed results
        scan_results = {
            "host": host,
            "ports_tested": ports,
            "total_ports": all_ports_tested,
            "open_ports": open_ports,
            "closed_ports": closed_ports,
            "failed_ports": failed_ports,
            "port_details": port_results,
            "total_scan_time_ms": round(total_time, 2),
            "success_rate": round((successful_ports / all_ports_tested) * 100, 1) if all_ports_tested > 0 else 0
        }
        
        if success:
            return TestResult(
                success=True,
                extra_detail={
                    **prerequisite_results,
                    "port_scan": {
                        "ok": True,
                        **scan_results
                    }
                }
            )
        else:
            # Create detailed failure advice
            advice = []
            if failed_ports:
                advice.append(f"Ports timing out (likely firewall blocked): {failed_ports}")
            if closed_ports:
                advice.append(f"Ports closed/refused (service not running): {closed_ports}")
            if open_ports:
                advice.append(f"Ports successfully connected: {open_ports}")
            
            advice.extend([
                "Check firewall rules on both client and server",
                "Verify services are running on expected ports",
                "Consider network security groups or iptables rules"
            ])
            
            error_msg = f"Port scan failed: {len(failed_ports)} timeouts, {len(closed_ports)} closed, {len(open_ports)} open"
            
            return TestResult(
                success=False,
                error_type="firewall_blocked" if failed_ports else "service_unavailable",
                error_message=error_msg,
                advice=advice,
                extra_detail={
                    **prerequisite_results,
                    "port_scan": {
                        "ok": False,
                        **scan_results
                    }
                }
            )


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypePortScan,
    shortname="port_scan",
    is_public=True,
    description="Tests connectivity to multiple ports to identify firewall blocking patterns"
)
