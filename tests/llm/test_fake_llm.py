# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
import json
from typing import AsyncGenerator, Dict, List, Optional, Generator, Any
from pathlib import Path
from totodev_pub.llm.fake_llm import FakeLLM
import asyncio
from langchain.schema import Generation, LLMResult
from langchain_core.outputs import ChatGeneration
from langchain_core.messages import BaseMessage
from pytest import FixtureRequest


def test_basic_response() -> None:
    """Test basic response functionality using a dictionary-style lambda"""
    responses: Dict[str, str] = {
        "hello": "world",
        "test": "response"
    }
    llm = FakeLLM(lambda p: responses.get(p, "default"))
    
    # Use invoke() instead of deprecated __call__
    assert llm.invoke("hello") == "world"
    assert llm.invoke("test") == "response"
    assert llm.invoke("unknown") == "default"

def test_function_based_response() -> None:
    """Test using a more complex response function"""
    def response_func(prompt: str) -> str:
        if "add" in prompt.lower():
            nums = [int(n) for n in prompt.split() if n.isdigit()]
            return str(sum(nums))
        if "weather" in prompt.lower():
            return "sunny"
        return "I don't understand"
    
    llm = FakeLLM(response_func)
    
    assert llm.invoke("add 2 and 3") == "5"
    assert llm.invoke("what's the weather?") == "sunny"
    assert llm.invoke("invalid prompt") == "I don't understand"

def test_json_mode() -> None:
    """Test JSON mode handling"""
    def json_response_func(prompt: str) -> str:
        if "name" in prompt:
            return json.dumps({"name": "Alice", "age": 30})
        return "not json"
    
    llm = FakeLLM(json_response_func)
    
    # Test with JSON mode
    response = llm.invoke("get name", response_format={"type": "json_object"})
    assert json.loads(response) == {"name": "Alice", "age": 30}
    
    # Test without JSON mode
    response = llm.invoke("get name")
    assert response == json.dumps({"name": "Alice", "age": 30})

def test_register_llm() -> None:
    """Test the register_llm class method"""
    from totodev_pub.llm.simplified_llm import SimplifiedLLM
    
    try:
        # Clear any existing registrations
        SimplifiedLLM._llm_makers.clear()
        
        canned_responses: Dict[str, str] = {
            "q1": "a1",
            "q2": "a2"
        }
        
        # Register the LLM - wrap dictionary in lambda with default response
        FakeLLM.register_llm(
            "TEST_LLM",
            lambda prompt: canned_responses.get(prompt, "default")
        )
        
        # Get the registered LLM through SimplifiedLLM
        llm, _ = SimplifiedLLM.create_llm_and_throttle("TEST_LLM")
        
        # Test responses
        assert llm.invoke("q1") == "a1"
        assert llm.invoke("q2") == "a2"
        assert llm.invoke("unknown") == "default"
    finally:
        # Clean up any remaining file locks
        SimplifiedLLM._llm_makers.clear()
        # Force garbage collection to ensure destructors are called
        import gc
        gc.collect()

@pytest.fixture(autouse=True)
def cleanup_llm_makers(request: FixtureRequest) -> Generator[None, None, None]:
    """Fixture to clean up LLM makers after each test"""
    yield
    from totodev_pub.llm.simplified_llm import SimplifiedLLM
    SimplifiedLLM._llm_makers.clear()
    import gc
    gc.collect()

def test_stateful_responses() -> None:
    """Test that the response function can maintain state"""
    class Counter:
        def __init__(self) -> None:
            self.count: int = 0
        
        def __call__(self, prompt: str) -> str:
            self.count += 1
            return f"Call count: {self.count}"
    
    counter = Counter()
    llm = FakeLLM(counter)
    
    # Use invoke() instead of deprecated __call__
    assert llm.invoke("test") == "Call count: 1"
    assert llm.invoke("test") == "Call count: 2"
    assert llm.invoke("test") == "Call count: 3"

def test_llm_properties() -> None:
    """Test LLM property methods"""
    llm = FakeLLM(lambda p: "test")
    
    assert llm._llm_type == "fake_llm"
    assert llm._identifying_params == {"fake_llm": True}

@pytest.mark.asyncio
async def test_async_call() -> None:
    """Test async call functionality"""
    responses: Dict[str, str] = {
        "hello": "world",
        "test": "response"
    }
    llm = FakeLLM(lambda p: responses.get(p, "default"))
    
    # Test async calls
    assert await llm.ainvoke("hello") == "world"
    assert await llm.ainvoke("test") == "response"
    assert await llm.ainvoke("unknown") == "default"

@pytest.mark.asyncio
async def test_async_batch() -> None:
    """Test async batch processing"""
    counter: int = 0
    
    async def async_response_func(prompt: str) -> str:
        nonlocal counter
        counter += 1
        await asyncio.sleep(0.1)  # Simulate async work
        return f"Response {counter} for: {prompt}"
    
    llm = FakeLLM(async_response_func)
    
    # Test multiple prompts in parallel
    prompts: List[str] = ["p1", "p2", "p3"]
    results = await asyncio.gather(*[llm.ainvoke(p) for p in prompts])
    
    assert len(results) == 3
    assert all(isinstance(r, str) for r in results)
    assert "Response" in results[0]

@pytest.mark.asyncio
async def test_async_streaming_with_stop() -> None:
    """Test async streaming with stop sequences"""
    pytest.skip("Skipping async streaming with stop sequence - need to fix this test case")
    test_response: str = "This is a test. STOP Here is more. STOP Final part."
    
    async def stream_with_stops(prompt: str) -> str:
        await asyncio.sleep(0.1)  # Simulate async work
        return test_response
    
    llm = FakeLLM(stream_with_stops)
    
    chunks: List[str] = []
    stop_sequences: List[str] = ["STOP"]
    
    # Use the generator directly without context manager
    async for chunk in llm.astream("test", stop=stop_sequences):
        chunks.append(chunk.text)  # Note: chunk is a GenerationChunk
        if any(stop in chunk.text for stop in stop_sequences):
            break
    
    assert "STOP" in chunks[-1]
    assert len(chunks) < len(test_response.split())

@pytest.mark.asyncio
async def test_async_error_handling() -> None:
    """Test async error handling"""
    async def failing_response(prompt: str) -> str:
        await asyncio.sleep(0.1)  # Simulate async work
        if "fail" in prompt:
            raise ValueError("Simulated failure")
        return "success"
    
    llm = FakeLLM(failing_response)
    
    # Test successful case
    assert await llm.ainvoke("test") == "success"
    
    # Test error case
    with pytest.raises(ValueError, match="Simulated failure"):
        await llm.ainvoke("fail") 