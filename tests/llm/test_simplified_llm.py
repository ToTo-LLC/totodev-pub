# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
from pydantic import BaseModel
from totodev_pub.llm.simplified_llm import SimplifiedLLM,_LLMInferenceCache, _LLMRequestLogger
from totodev_pub.llm.fake_llm import FakeLLM
import os
import time
from datetime import datetime, timedelta
from totodev_pub.pytest_tools import very_lazy_test
import asyncio
import tempfile
import csv
from unittest.mock import patch, MagicMock
from copy import deepcopy


# Simplified canned responses - exact matches for prompts
CANNED_RESPONSES = {
    "Who wrote Hamlet?": "William Shakespeare wrote Hamlet.",
    "What is 2+2?": '{"answer": 4}',
    "Add 2 and 3": '{"first_number": 2, "second_number": 3, "sum": 5}'
}

@pytest.fixture(scope="session", autouse=True)
def setup_fake_llm(canned_responses: dict = CANNED_RESPONSES):
    """
    Register fake LLM for testing.
    Note that the canned responses passed in are retained by reference so that if you update the dictionary, the responses of the fake LLM will change.
    """
    def get_response(prompt: str) -> str:
        """Response generator function for test LLM"""
        if prompt.startswith('Human: '):
            prompt = prompt[7:].strip()
        return canned_responses.get(prompt, "--UNHANDLED RESPONSE TO FAKE LLM--")

    # Register main test LLM
    SimplifiedLLM.register_fake_llm_maker(
        config_name="FAKE_LLM",
        response_func=get_response
    )


class MathAnswerTestingStruct(BaseModel):
    first_number: int
    second_number: int
    sum: int

@pytest.mark.asyncio
async def test_llm_responses():
    """Test various response types in one async test"""
    # Test string response
    str_response = await SimplifiedLLM.a_answer_str(
        "Who wrote Hamlet?",
        config_name="FAKE_LLM"
    )
    assert "Shakespeare" in str_response

    # Test JSON response with json_mode=True
    json_response = await SimplifiedLLM.a_answer_struct(
        "What is 2+2?",
        None,
        config_name="FAKE_LLM",
        json_mode=True
    )
    assert json_response['answer'] == 4

    # Test Pydantic model response with json_mode=True
    model_response = await SimplifiedLLM.a_answer_struct(
        "Add 2 and 3",
        MathAnswerTestingStruct,
        config_name="FAKE_LLM",
        json_mode=True
    )
    assert isinstance(model_response, MathAnswerTestingStruct)
    assert model_response.sum == 5

def test_llm_config():
    """Test LLM configuration and creation"""
    # Test available configs
    configs = SimplifiedLLM.available_llm_configs()
    assert "FAKE_LLM" in configs

    # Test LLM creation
    llm, throttle = SimplifiedLLM.create_llm_and_throttle("FAKE_LLM")
    assert isinstance(llm, FakeLLM)
    assert throttle is None  # since we didn't request a throttle on registering the LLM this will be None

    # Test invalid config
    with pytest.raises(ValueError):
        SimplifiedLLM.create_llm_and_throttle(
            "nonexistent",
            default_model_fallback=False
        )

@pytest.mark.asyncio
async def test_parallel_requests():
    """Test parallel request handling with minimal input"""
    responses = await SimplifiedLLM.a_answer_struct(
        "Add 2 and 3",
        MathAnswerTestingStruct,
        config_name="FAKE_LLM",
        parallel=2,
        json_mode=True
    )
    assert len(responses) == 2
    assert all(isinstance(r, MathAnswerTestingStruct) for r in responses)
    assert all(r.sum == 5 for r in responses)

@pytest.fixture
def temp_cache_file(tmp_path):
    """Create a temporary cache file for testing.
    
    This fixture ensures the test cache file is always cleaned up, even if tests fail.
    """
    cache_file = tmp_path / "test_cache.db"
    cache_path = str(cache_file)
    try:
        yield cache_path
    finally:
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
            # Also try to clean up any -journal or -wal files that SQLite might create
            for ext in ['-journal', '-wal', '-shm']:
                journal_file = cache_path + ext
                if os.path.exists(journal_file):
                    os.remove(journal_file)
        except Exception as e:
            # Log but don't raise - we don't want cleanup errors to mask test failures
            import logging
            logging.warning(f"Failed to cleanup test cache file {cache_path}: {str(e)}")

@pytest.fixture
def temp_cache_file2(tmp_path):
    """Create a second temporary cache file for testing multiple caches.
    
    This fixture ensures the test cache file is always cleaned up, even if tests fail.
    """
    cache_file = tmp_path / "test_cache2.db"
    cache_path = str(cache_file)
    try:
        yield cache_path
    finally:
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
            # Also try to clean up any -journal or -wal files that SQLite might create
            for ext in ['-journal', '-wal', '-shm']:
                journal_file = cache_path + ext
                if os.path.exists(journal_file):
                    os.remove(journal_file)
        except Exception as e:
            # Log but don't raise - we don't want cleanup errors to mask test failures
            import logging
            logging.warning(f"Failed to cleanup test cache file {cache_path}: {str(e)}")

def test_cache_initialization(temp_cache_file):
    """Test cache initialization with different parameters"""
    # Test default initialization
    cache = _LLMInferenceCache(temp_cache_file)
    assert cache.cache_minutes == 30
    assert cache.compress_strings == True

    # Test custom initialization
    cache = _LLMInferenceCache(
        temp_cache_file,
        cache_minutes=60,
        compress_strings=False
    )
    assert cache.cache_minutes == 60
    assert cache.compress_strings == False

def test_cache_store_and_retrieve(temp_cache_file):
    """Test basic storage and retrieval of cache entries"""
    cache = _LLMInferenceCache(temp_cache_file)
    
    # Test with simple string
    prompt = "test prompt"
    response = "test response"
    cache.store_entry(prompt, response)
    assert cache.get_entry(prompt) == response

    # Test with longer text containing special characters
    prompt2 = "What is the meaning of life?\nTell me in detail!"
    response2 = "The meaning of life is...\n42\n¯\\_(ツ)_/¯"
    cache.store_entry(prompt2, response2)
    assert cache.get_entry(prompt2) == response2

def test_cache_compression(temp_cache_file):
    """Test that compression works correctly"""
    # Test with compression
    cache_compressed = _LLMInferenceCache(temp_cache_file, compress_strings=True)
    prompt = "test prompt" * 100  # Make it longer to better test compression
    response = "test response" * 100
    cache_compressed.store_entry(prompt, response)
    assert cache_compressed.get_entry(prompt) == response

    # Test without compression
    cache_uncompressed = _LLMInferenceCache(temp_cache_file, compress_strings=False)
    cache_uncompressed.store_entry(prompt, response)
    assert cache_uncompressed.get_entry(prompt) == response

@pytest.mark.slow
@very_lazy_test(['totodev_pub.llm.simplified_llm'], random_period=20)
def test_cache_expiration(temp_cache_file):
    """Test that cache entries expire correctly."""
    # Create cache with very short expiration (2 seconds)
    cache = _LLMInferenceCache(temp_cache_file, cache_seconds=2)  # Changed to use seconds
    
    prompt = "test prompt"
    response = "test response"
    cache.store_entry(prompt, response)
    
    # Should be available immediately
    assert cache.get_entry(prompt) == response
    
    # Wait for expiration
    time.sleep(2.1)  # Wait just over 2 seconds
    
    # Should return None after expiration
    assert cache.get_entry(prompt) is None

def test_cache_purge(temp_cache_file):
    """Test purging functionality"""
    cache = _LLMInferenceCache(temp_cache_file)
    
    # Store multiple entries
    entries = [
        ("prompt1", "response1"),
        ("prompt2", "response2"),
        ("prompt3", "response3")
    ]
    for prompt, response in entries:
        cache.store_entry(prompt, response)
    
    # Verify all entries are stored
    for prompt, response in entries:
        assert cache.get_entry(prompt) == response
    
    # Test purging single entry
    cache.purge("prompt1")
    assert cache.get_entry("prompt1") is None
    assert cache.get_entry("prompt2") == "response2"  # Other entries should remain
    
    # Test purging all entries
    cache.purge()
    for prompt, _ in entries:
        assert cache.get_entry(prompt) is None

@pytest.mark.slow
@very_lazy_test(['totodev_pub.llm.simplified_llm'],random_period=20)
def test_cache_periodic_purge(temp_cache_file):
    """Test that periodic purge works."""
    # Use seconds instead of minutes for faster testing, but avoid sub-second timing
    cache = _LLMInferenceCache(temp_cache_file, cache_seconds=2)  # Changed from 0.5 to 2 seconds

    # Store some entries
    cache.store_entry("prompt1", "response1")
    cache.store_entry("prompt2", "response2")

    # Force a periodic purge by waiting
    time.sleep(3)  # Changed from 0.6 to 3 seconds to ensure expiration

    # Store a new entry to trigger periodic purge
    cache.store_entry("prompt3", "response3")

    # Old entries should be gone, new entry should remain
    assert cache.get_entry("prompt1") is None
    assert cache.get_entry("prompt2") is None
    assert cache.get_entry("prompt3") == "response3"

def test_register_maker_alias(temp_cache_file):
    """Test registering LLM aliases with and without caching"""
    # Create a dictionary that will be bound by reference to the FakeLLM
    responses = {
        "test_prompt": "initial response"
    }
    
    try:
        # Register base LLM that uses the responses dictionary
        FakeLLM.register_llm(
            "BASE_LLM",
            lambda p: responses.get(p, "unknown")
        )
        
        # Create cached and uncached aliases
        SimplifiedLLM.register_maker_alias(
            config_name="BASE_LLM",
            alias_config_name="DIRECT_LLM"  # uncached version
        )
        
        SimplifiedLLM.register_maker_alias(
            config_name="BASE_LLM",
            alias_config_name="CACHED_LLM",
            cache_file=temp_cache_file,
            cache_minutes=30
        )
        
        # Test initial responses - both should match
        direct_response = asyncio.run(SimplifiedLLM.a_answer_str("test_prompt", "DIRECT_LLM"))
        cached_response = asyncio.run(SimplifiedLLM.a_answer_str("test_prompt", "CACHED_LLM"))
        assert direct_response == "initial response"
        assert cached_response == "initial response"
        
        # Modify the underlying dictionary
        responses["test_prompt"] = "updated response"
        
        # Test responses after modification
        direct_response = asyncio.run(SimplifiedLLM.a_answer_str("test_prompt", "DIRECT_LLM"))
        cached_response = asyncio.run(SimplifiedLLM.a_answer_str("test_prompt", "CACHED_LLM"))
        
        # Direct should see new value, cached should retain old value
        assert direct_response == "updated response"
        assert cached_response == "initial response"
        
    finally:
        # Clean up registered LLMs
        SimplifiedLLM._llm_makers.clear()

@pytest.mark.asyncio
async def test_register_maker_alias_async(temp_cache_file):
    """Test async behavior of cached and uncached aliases"""
    try:
        # Register base LLM with async response
        async def async_response(prompt: str) -> str:
            await asyncio.sleep(0.1)  # Simulate API delay
            if prompt.startswith('Human: '):
                prompt = prompt[7:].strip()
            return f"Async response to: {prompt}"
        
        FakeLLM.register_llm(
            "ASYNC_BASE_LLM",
            async_response
        )
        
        # Register aliases
        SimplifiedLLM.register_maker_alias(
            config_name="ASYNC_BASE_LLM",
            alias_config_name="ASYNC_UNCACHED"
        )
        
        SimplifiedLLM.register_maker_alias(
            config_name="ASYNC_BASE_LLM",
            alias_config_name="ASYNC_CACHED",
            cache_file=temp_cache_file,
            cache_minutes=30
        )
        
        # Test async responses
        response1 = await SimplifiedLLM.a_answer_str(
            "test prompt",
            config_name="ASYNC_UNCACHED"
        )
        assert response1 == "Async response to: test prompt"
        
        response2 = await SimplifiedLLM.a_answer_str(
            "test prompt",
            config_name="ASYNC_CACHED"
        )
        assert response2 == "Async response to: test prompt"
    finally:
        # Clean up registered LLMs
        SimplifiedLLM._llm_makers.clear()

def test_register_maker_alias_errors():
    """Test error conditions for register_maker_alias"""
    try:
        # Test registering alias for non-existent config
        with pytest.raises(ValueError, match="not found in registered LLM makers"):
            SimplifiedLLM.register_maker_alias(
                config_name="NONEXISTENT_LLM",
                alias_config_name="BAD_ALIAS"
            )
        
        # Test registering duplicate alias name
        FakeLLM.register_llm(
            "BASE_FOR_ERROR_TEST",
            lambda p: "test"
        )
        
        SimplifiedLLM.register_maker_alias(
            config_name="BASE_FOR_ERROR_TEST",
            alias_config_name="EXISTING_ALIAS"
        )
        
        with pytest.raises(ValueError, match="already exists"):
            SimplifiedLLM.register_maker_alias(
                config_name="BASE_FOR_ERROR_TEST",
                alias_config_name="EXISTING_ALIAS"
            )
    finally:
        # Clean up registered LLMs
        SimplifiedLLM._llm_makers.clear()

@pytest.mark.asyncio
async def test_request_logging():
    """Test that requests are properly logged by SimplifiedLLM."""
    # Create a temporary log file
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_path = temp_file.name
    
    try:
        # Set up request logging
        SimplifiedLLM.attach_logfile(temp_path)
        
        # Register the fake LLM
        FakeLLM.register_llm(
            config_name="FAKE_LLM",
            response_generator=lambda prompt: CANNED_RESPONSES.get(prompt.replace('Human: ', '').strip(), "--UNHANDLED RESPONSE TO FAKE LLM--")
        )
        
        # Test successful request with explicit logging_label
        await SimplifiedLLM.a_answer_str(
            "Who wrote Hamlet?",
            config_name="FAKE_LLM",
            logging_label="test_success"
        )
        
        # Test successful request with default logging_label
        await SimplifiedLLM.a_answer_str(
            "Who wrote Hamlet?",
            config_name="FAKE_LLM"
        )
        
        # Test failed request
        with pytest.raises(Exception):
            await SimplifiedLLM.a_answer_str(
                "This will fail",
                config_name="NONEXISTENT_LLM",
                default_model_fallback=False,
                logging_label="test_failure"
            )
        
        # Verify log file contents
        with open(temp_path, 'r') as f:
            reader = csv.reader(f)
            rows = list(reader)
            
            # First row should be header
            assert rows[0] == ["timestamp", "logging_label", "config_name", "char_count", "duration_seconds"]
            
            # Second row should be successful request with explicit label
            assert rows[1][1] == "test_success"
            assert rows[1][2] == "FAKE_LLM"
            assert rows[1][3] == "17"  # "Who wrote Hamlet?" is 17 chars
            assert float(rows[1][4]) > 0  # Duration should be positive
            
            # Third row should be successful request with default label
            assert rows[2][1] == "~"  # Default label
            assert rows[2][2] == "FAKE_LLM"
            assert rows[2][3] == "17"
            assert float(rows[2][4]) > 0
            
            # Fourth row should be failed request
            assert rows[3][1] == "test_failure"
            assert rows[3][2] == "NONEXISTENT_LLM"
            assert rows[3][3] == "14"  # "This will fail" is 14 chars
            assert rows[3][4] == ""  # Empty duration for failed request
    
    finally:
        # Clean up
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        # Reset request logger
        SimplifiedLLM._request_logger = None
        # Clean up registered LLMs
        SimplifiedLLM._llm_makers.clear()


@pytest.mark.asyncio
async def test_request_logging_struct():
    """Test that structured requests are properly logged by SimplifiedLLM."""
    # Create a temporary log file
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_path = temp_file.name
    
    try:
        # Set up request logging
        SimplifiedLLM.attach_logfile(temp_path)
        
        # Register the fake LLM
        FakeLLM.register_llm(
            config_name="FAKE_LLM",
            response_generator=lambda prompt: CANNED_RESPONSES.get(prompt.replace('Human: ', '').strip(), "--UNHANDLED RESPONSE TO FAKE LLM--")
        )
        
        # Test successful structured request
        await SimplifiedLLM.a_answer_struct(
            "Add 2 and 3",
            pydantic_data_class=MathAnswerTestingStruct,
            config_name="FAKE_LLM",
            logging_label="test_struct"
        )
        
        # Verify log file contents
        with open(temp_path, 'r') as f:
            reader = csv.reader(f)
            rows = list(reader)
            
            # Check the row for the structured request
            assert any(row[1] == "test_struct" and row[2] == "FAKE_LLM" for row in rows)
            # Find the actual character count
            struct_row = next((row for row in rows if row[1] == "test_struct"), None)
            if struct_row:
                pass
    
    finally:
        # Clean up
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        # Reset request logger
        SimplifiedLLM._request_logger = None
        # Clean up registered LLMs
        SimplifiedLLM._llm_makers.clear()

def test_maker_config_storage_and_retrieval():
    """Test the enhanced maker configuration storage and retrieval functionality"""
    try:
        # Clear any existing registrations
        SimplifiedLLM._llm_makers.clear()
        SimplifiedLLM._pkd_maker_params.clear()
        
        # Test basic registration and retrieval
        original_config = {
            "protocol": "openai",
            "api_key": "test-key",
            "model_name": "gpt-4",
            "CharsPerMinThrottle": "1000"  # Deliberately camelCase to test processing
        }
        
        SimplifiedLLM.register_maker_by_pkd("TEST_LLM", original_config)
        
        # Test basic retrieval
        basic_config = SimplifiedLLM.get_maker_config("TEST_LLM")
        assert basic_config == original_config
        
        # Test detailed retrieval
        detailed_config = SimplifiedLLM.get_maker_config("TEST_LLM", include_processed=True)
        assert detailed_config['original'] == original_config
        assert detailed_config['protocol'] == 'openai'
        assert detailed_config['processed']['chars_per_min_throttle'] == 1000  # Verify snake_case and type conversion
        assert not detailed_config['is_alias']
        assert detailed_config['alias_of'] is None
        
        # Test alias handling
        SimplifiedLLM.register_maker_alias("TEST_LLM", "TEST_LLM_ALIAS")
        
        alias_config = SimplifiedLLM.get_maker_config("TEST_LLM_ALIAS", include_processed=True)
        assert alias_config['original'] == original_config
        assert alias_config['is_alias']
        assert alias_config['alias_of'] == "TEST_LLM"
        
        # Test error handling for non-existent config
        with pytest.raises(ValueError, match="No configuration named 'NONEXISTENT' exists"):
            SimplifiedLLM.get_maker_config("NONEXISTENT")
        
        # Test error handling for invalid protocol
        with pytest.raises(ValueError, match="Unknown protocol"):
            SimplifiedLLM.register_maker_by_pkd("INVALID", {"protocol": "invalid_protocol", "api_key": "test"})
            
        # Verify that failed registration doesn't store config
        assert "INVALID" not in SimplifiedLLM._pkd_maker_params
        
    finally:
        # Clean up
        SimplifiedLLM._llm_makers.clear()
        SimplifiedLLM._pkd_maker_params.clear()

def test_maker_config_error_handling():
    """Test error handling in maker configuration management"""
    try:
        # Clear any existing registrations
        SimplifiedLLM._llm_makers.clear()
        SimplifiedLLM._pkd_maker_params.clear()
        
        # Test missing protocol
        with pytest.raises(ValueError, match="must contain a 'protocol' key"):
            SimplifiedLLM.register_maker_by_pkd("TEST_LLM", {"api_key": "test-key"})
        
        # Register a base configuration
        base_config = {
            "protocol": "openai",
            "api_key": "test-key",
            "model_name": "gpt-4"
        }
        SimplifiedLLM.register_maker_by_pkd("BASE_LLM", base_config)
        
        # Create an alias but don't register its base via register_maker_by_pkd
        SimplifiedLLM._llm_makers.clear()
        SimplifiedLLM._pkd_maker_params.clear()
        
        # Register a new LLM directly (not via register_maker_by_pkd)
        FakeLLM.register_llm("DIRECT_LLM", lambda p: "test")
        
        # Try to get config for LLM not registered via register_maker_by_pkd
        with pytest.raises(KeyError, match="exists but was not registered using register_maker_by_pkd"):
            SimplifiedLLM.get_maker_config("DIRECT_LLM")
            
        # Create an alias to it
        SimplifiedLLM.register_maker_alias("DIRECT_LLM", "ALIAS_LLM")
        
        # Try to get config for alias of LLM not registered via register_maker_by_pkd
        with pytest.raises(KeyError, match="was not registered using register_maker_by_pkd"):
            SimplifiedLLM.get_maker_config("ALIAS_LLM")
            
    finally:
        # Clean up
        SimplifiedLLM._llm_makers.clear()
        SimplifiedLLM._pkd_maker_params.clear()

def test_register_maker_by_pkd_preserves_input_dict():
    """Test that register_maker_by_pkd does not modify the input dictionary and handles allow_load_balanced correctly."""
    
    # Setup - create a response function for our fake LLM
    def fake_response(prompt: str) -> str:
        return "fake response"

    # Create original config
    original_config = {
        "Protocol": "fake",
        "response_func": fake_response,
        "allow_load_balanced": "true",
        "chars_per_min_throttle": "1000"
    }
    
    # Make a deep copy to compare later
    config_copy = deepcopy(original_config)
    
    # Test 1: Register with config's allow_load_balanced value
    SimplifiedLLM.register_maker_by_pkd("test_config1", original_config)
    
    # Verify the original dict was not modified
    assert original_config == config_copy, "Input dictionary was modified"
    
    # Test 2: Override with explicit allow_load_balanced=False
    SimplifiedLLM.register_maker_by_pkd("test_config2", original_config, allow_load_balanced=False)
    
    # Verify the original dict was still not modified
    assert original_config == config_copy, "Input dictionary was modified when overriding allow_load_balanced"
    
    # Test 3: Verify load balancing behavior
    # First registration with load balancing enabled
    SimplifiedLLM.register_maker_by_pkd("test_config3", original_config, allow_load_balanced=True)
    
    # Second registration should succeed due to load balancing
    SimplifiedLLM.register_maker_by_pkd("test_config3", original_config, allow_load_balanced=True)
    
    # Verify we have a load balanced maker
    maker = SimplifiedLLM._llm_makers["test_config3"]
    assert hasattr(maker, 'makers'), "Load balancing was not enabled"
    assert len(maker.makers) == 2, "Expected 2 makers in load balancer"
    
    # Test 4: Verify load balancing is prevented when disabled
    # First registration with load balancing disabled
    SimplifiedLLM.register_maker_by_pkd("test_config4", original_config, allow_load_balanced=False)
    
    # Second registration should fail
    with pytest.raises(ValueError, match=".*already exists.*"):
        SimplifiedLLM.register_maker_by_pkd("test_config4", original_config, allow_load_balanced=False)
    
    # Cleanup
    SimplifiedLLM._llm_makers.clear()
    SimplifiedLLM._pkd_maker_params.clear()

