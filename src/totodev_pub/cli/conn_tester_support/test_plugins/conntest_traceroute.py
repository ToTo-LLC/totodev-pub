#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Traceroute network path analysis test plugin.

This module provides network path tracing functionality to identify where
connectivity fails along the route to a destination.
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
from typing import Any, Dict, List, Optional, Type

from conn_tester_support.core import TestTypeBase
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve


class TestTypeTraceroute(TestTypeBase):
    """Network path tracing to identify where connectivity fails"""
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Traces network path to identify where connectivity fails (requires traceroute/tracert)",
            config_fields={
                "host": "Target hostname or IP address",
                "max_hops": "Maximum number of hops to trace (default: 30)",
                "timeout_s": "Timeout per hop in seconds (default: 5)"
            },
            required_fields=["host"],
            optional_fields=["max_hops", "timeout_s"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve]
    
    def _find_traceroute_command(self) -> Optional[str]:
        """Find the appropriate traceroute command for the current platform"""
        system = platform.system().lower()
        
        if system == "windows":
            # Windows uses tracert
            commands = ["tracert.exe", "tracert"]
        else:
            # Unix-like systems use traceroute
            commands = ["traceroute"]
        
        # Check if command exists in PATH
        for cmd in commands:
            try:
                result = subprocess.run(
                    ["which" if system != "windows" else "where", cmd],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    return cmd
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
        
        # Check common installation paths
        if system == "windows":
            common_paths = [
                r"C:\Windows\System32\tracert.exe",
                r"C:\Windows\SysWOW64\tracert.exe"
            ]
        else:
            common_paths = [
                "/usr/bin/traceroute",
                "/bin/traceroute",
                "/usr/sbin/traceroute",
                "/sbin/traceroute"
            ]
        
        for path in common_paths:
            if os.path.exists(path) and os.access(path, os.X_OK):
                return path
        
        return None
    
    def _build_traceroute_command(self, host: str, max_hops: int, timeout_s: int) -> List[str]:
        """Build the appropriate traceroute command for the platform"""
        system = platform.system().lower()
        traceroute_cmd = self._find_traceroute_command()
        
        if not traceroute_cmd:
            return None
        
        if system == "windows":
            # Windows tracert syntax: tracert -h max_hops -w timeout_ms target
            cmd = [traceroute_cmd, "-h", str(max_hops), "-w", str(timeout_s * 1000), host]
        else:
            # Unix traceroute syntax: traceroute -m max_hops -w timeout_s target
            cmd = [traceroute_cmd, "-m", str(max_hops), "-w", str(timeout_s), host]
        
        return cmd
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        # Validate configuration
        host = self.config.get("host")
        max_hops = self.config.get("max_hops") or 30  # Default 30 hops if None or not provided
        timeout_s = self.config.get("timeout_s") or 5  # Default 5 seconds if None or not provided
        
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
        
        # Validate max_hops
        try:
            max_hops = int(max_hops)
            if not (1 <= max_hops <= 255):
                return TestResult(
                    success=False,
                    error_type="config_invalid",
                    error_message=f"Invalid max_hops value: {max_hops}",
                    advice=["max_hops must be between 1 and 255"]
                )
        except (ValueError, TypeError):
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid max_hops value: {max_hops}",
                advice=["max_hops must be a valid integer"]
            )
        
        # Validate timeout
        try:
            timeout_s = int(timeout_s)
            if timeout_s <= 0:
                return TestResult(
                    success=False,
                    error_type="config_invalid",
                    error_message=f"Invalid timeout value: {timeout_s}",
                    advice=["timeout_s must be a positive integer"]
                )
        except (ValueError, TypeError):
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message=f"Invalid timeout value: {timeout_s}",
                advice=["timeout_s must be a valid integer"]
            )
        
        # Check if traceroute command is available
        traceroute_cmd = self._find_traceroute_command()
        if not traceroute_cmd:
            system = platform.system().lower()
            if system == "windows":
                install_advice = "tracert should be available by default on Windows"
            else:
                install_advice = "Install traceroute: sudo apt-get install traceroute (Ubuntu/Debian) or sudo yum install traceroute (CentOS/RHEL)"
            
            return TestResult(
                success=False,
                error_type="tool_unavailable",
                error_message="Traceroute command not found on this system",
                advice=[
                    "Traceroute is required for network path analysis",
                    install_advice,
                    "Ensure traceroute is in your PATH"
                ]
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
        
        # Build traceroute command
        cmd = self._build_traceroute_command(host, max_hops, timeout_s)
        if not cmd:
            return TestResult(
                success=False,
                error_type="tool_unavailable",
                error_message="Failed to build traceroute command",
                advice=["Check traceroute installation and permissions"]
            )
        
        # Execute traceroute with real-time output capture
        hops = []
        output_lines = []
        reached_destination = False
        start_time = time.time()
        
        try:
            # Log the command being executed
            if logger:
                logger.info(f"Executing: {' '.join(cmd)}")
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # Read output line by line in real-time
            hop_number = 0
            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue
                
                output_lines.append(line)
                
                # Log intermediate output
                if logger:
                    logger.info(f"Traceroute: {line}")
                
                # Parse hop information (basic parsing, varies by platform)
                if self._is_hop_line(line):
                    hop_number += 1
                    hop_info = self._parse_hop_line(line, hop_number)
                    hops.append(hop_info)
                    
                    # Check if we reached the destination
                    if host.lower() in line.lower() or self._is_destination_reached(line, host):
                        reached_destination = True
            
            process.wait()
            end_time = time.time()
            
            total_time_ms = (end_time - start_time) * 1000
            
            # Determine success based on whether we reached the destination
            if reached_destination:
                return TestResult(
                    success=True,
                    extra_detail={
                        **prerequisite_results,
                        "traceroute": {
                            "ok": True,
                            "destination": host,
                            "hops": hops,
                            "total_hops": len(hops),
                            "reached_destination": True,
                            "total_time_ms": round(total_time_ms, 2),
                            "command_used": ' '.join(cmd),
                            "raw_output": output_lines
                        }
                    }
                )
            else:
                return TestResult(
                    success=False,
                    error_type="network_unreachable",
                    error_message=f"Failed to reach destination {host}",
                    advice=[
                        f"Traceroute completed {len(hops)} hops but did not reach {host}",
                        "Check for firewall rules blocking ICMP or UDP packets",
                        "Destination may be unreachable or blocking traceroute probes",
                        "Review hop details to identify where the path fails"
                    ],
                    extra_detail={
                        **prerequisite_results,
                        "traceroute": {
                            "ok": False,
                            "destination": host,
                            "hops": hops,
                            "total_hops": len(hops),
                            "reached_destination": False,
                            "total_time_ms": round(total_time_ms, 2),
                            "command_used": ' '.join(cmd),
                            "raw_output": output_lines
                        }
                    }
                )
                
        except subprocess.TimeoutExpired:
            return TestResult(
                success=False,
                error_type="timeout",
                error_message="Traceroute command timed out",
                advice=[
                    "Traceroute took too long to complete",
                    "Try reducing max_hops or increasing timeout_s",
                    "Network path may have routing loops or very slow links"
                ],
                extra_detail={
                    **prerequisite_results,
                    "traceroute": {
                        "ok": False,
                        "destination": host,
                        "hops": hops,
                        "total_hops": len(hops),
                        "reached_destination": False,
                        "timeout": True,
                        "command_used": ' '.join(cmd),
                        "raw_output": output_lines
                    }
                }
            )
        except Exception as e:
            return TestResult(
                success=False,
                error_type="execution_error",
                error_message=f"Error executing traceroute: {e}",
                advice=[
                    "Check traceroute permissions (may require sudo on some systems)",
                    "Verify traceroute is properly installed",
                    "Check system firewall settings"
                ],
                extra_detail={
                    **prerequisite_results,
                    "traceroute": {
                        "ok": False,
                        "destination": host,
                        "error": str(e),
                        "command_used": ' '.join(cmd) if cmd else "unknown"
                    }
                }
            )
    
    def _is_hop_line(self, line: str) -> bool:
        """Check if a line contains hop information"""
        # Basic heuristic - lines that start with a number followed by whitespace
        # This works for most traceroute implementations
        line = line.strip()
        if not line:
            return False
        
        # Look for pattern like "1  " or " 1 " at the beginning
        parts = line.split()
        if parts and parts[0].isdigit():
            return True
        
        return False
    
    def _parse_hop_line(self, line: str, hop_number: int) -> Dict[str, Any]:
        """Parse a hop line to extract basic information"""
        # Basic parsing - extract what we can from the line
        # This is simplified and may not capture all information
        
        hop_info = {
            "hop": hop_number,
            "raw_line": line.strip()
        }
        
        # Try to extract IP addresses and hostnames
        parts = line.split()
        for part in parts:
            # Look for IP addresses (basic IPv4 pattern)
            if '.' in part and part.replace('.', '').replace('(', '').replace(')', '').isdigit():
                hop_info["ip"] = part.strip('()')
            # Look for timing information (ends with 'ms')
            elif part.endswith('ms'):
                try:
                    timing = float(part[:-2])
                    if "timings" not in hop_info:
                        hop_info["timings"] = []
                    hop_info["timings"].append(timing)
                except ValueError:
                    pass
        
        return hop_info
    
    def _is_destination_reached(self, line: str, destination: str) -> bool:
        """Check if the line indicates we've reached the destination"""
        line_lower = line.lower()
        dest_lower = destination.lower()
        
        # Look for the destination hostname or IP in the line
        if dest_lower in line_lower:
            return True
        
        # Look for completion indicators
        completion_indicators = ["reached", "arrived", "complete"]
        return any(indicator in line_lower for indicator in completion_indicators)


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeTraceroute,
    shortname="traceroute",
    is_public=True,
    description="Traces network path to identify where connectivity fails (requires traceroute/tracert)"
)
