#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
HTTP connection test plugin.

This module provides HTTP connectivity testing functionality,
including prerequisite DNS and TCP connectivity checks.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Type

from conn_tester_support.core import TestTypeBase, DEFAULT_HTTP_TIMEOUT, HTTP_PORT, _normalize_url_for_test
from conn_tester_support.models import TestMetadata, TestResult, ConfigurationError
from conn_tester_support.test_plugins.conntest_dns import TestTypeDnsResolve
from conn_tester_support.test_plugins.conntest_http_responds import TestTypeHttpResponds


class TestTypeHttp(TestTypeBase):
    """HTTP connectivity and response testing"""
    
    @classmethod
    def describe_self(cls) -> TestMetadata:
        return TestMetadata(
            description="Tests HTTP connectivity and retrieves URL content",
            config_fields={
                "url": "Target URL to retrieve",
                "method": "HTTP method (default: GET)",
                "headers": "HTTP headers as JSON string",
                "timeout_s": "Request timeout in seconds",
                "verify_tls": "Verify TLS certificate (default: false for HTTP)"
            },
            required_fields=["url"],
            optional_fields=["method", "headers", "timeout_s", "verify_tls"]
        )
    
    @classmethod
    def prerequisite_tests(cls) -> List[Type['TestTypeBase']]:
        return [TestTypeDnsResolve, TestTypeHttpResponds]
    
    def run_test(self, logger: Optional[Any] = None) -> TestResult:
        # Validate configuration
        if not self.config.get("url"):
            raise ConfigurationError("url is required")
        
        url = self.config.get("url")
        # Normalize URL to ensure it has http:// protocol if not already specified
        url = _normalize_url_for_test(url, "http")
        
        # Check for loopback addresses in URL
        if self.contains_loopback(url):
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
        method = self.config.get("method", "GET")
        headers = self.config.get("headers", "{}")
        timeout_s = self.config.get("timeout_s", DEFAULT_HTTP_TIMEOUT)
        
        # Parse headers if provided as JSON string
        try:
            if isinstance(headers, str):
                headers_dict = json.loads(headers)
            else:
                headers_dict = headers
        except json.JSONDecodeError:
            return TestResult(
                success=False,
                error_type="config_invalid",
                error_message="Invalid JSON in headers field",
                advice=["Provide headers as valid JSON", "Use empty object {} for no headers"]
            )
        
        # Run prerequisite tests
        prerequisite_results = {}
        
        # DNS resolution
        dns_test = TestTypeDnsResolve({"hostname": urllib.parse.urlparse(url).hostname})
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
        
        # HTTP port connectivity
        host = urllib.parse.urlparse(url).hostname
        http_test = TestTypeHttpResponds({"host": host, "port": HTTP_PORT})
        http_result = http_test.run_test()
        prerequisite_results.update(http_result.extra_detail)
        
        if not http_result.success:
            return TestResult(
                success=False,
                error_type="tcp_connect",
                error_message="HTTP port connectivity failed",
                advice=http_result.advice,
                extra_detail=prerequisite_results
            )
        
        # Main HTTP request
        try:
            start_time = time.time()
            request = urllib.request.Request(url, headers=headers_dict, method=method)
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                content = response.read()
                end_time = time.time()
            
            response_time_ms = (end_time - start_time) * 1000
            
            return TestResult(
                success=True,
                extra_detail={
                    **prerequisite_results,
                    "http_response": {
                        "status_code": response.getcode(),
                        "headers": dict(response.headers),
                        "response_time_ms": round(response_time_ms, 2),
                        "content_length": len(content)
                    }
                }
            )
        except urllib.error.HTTPError as e:
            return TestResult(
                success=False,
                error_type="http_error",
                error_message=f"HTTP error {e.code}: {e.reason}",
                advice=["Check URL validity", "Verify server is responding"],
                extra_detail={
                    **prerequisite_results,
                    "http_response": {
                        "status_code": e.code,
                        "error": str(e)
                    }
                }
            )
        except urllib.error.URLError as e:
            return TestResult(
                success=False,
                error_type="network_error",
                error_message=f"URL error: {e}",
                advice=["Check network connectivity", "Verify URL format"],
                extra_detail={
                    **prerequisite_results,
                    "http_response": {
                        "error": str(e)
                    }
                }
            )
        except Exception as e:
            return TestResult(
                success=False,
                error_type="network_error",
                error_message=f"Unexpected error during HTTP request: {e}",
                advice=["Check network connectivity", "Try again later"],
                extra_detail={
                    **prerequisite_results,
                    "http_response": {
                        "error": str(e)
                    }
                }
            )
    
    def get_configs(self) -> Dict[str, Any]:
        """Return current configuration with defaults applied"""
        config = self.config.copy()
        
        # Apply defaults for parameters that weren't explicitly set
        if config.get("method") is None:
            config["method"] = "GET"
        if config.get("headers") is None:
            config["headers"] = "{}"
        if config.get("timeout_s") is None:
            config["timeout_s"] = DEFAULT_HTTP_TIMEOUT
        if config.get("verify_tls") is None:
            config["verify_tls"] = False
        
        return config


# Auto-register when imported
TestTypeBase.register_test(
    test_class=TestTypeHttp,
    shortname="http",
    is_public=True,
    description="Tests HTTP connectivity and retrieves URL content"
)
