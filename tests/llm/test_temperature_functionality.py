# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for temperature functionality in SimplifiedLLM:
- Registering LLM makers with default_temperature
- a_answer_str with and without explicit temperature
- a_answer_struct with and without explicit temperature
- Verifying temperature is properly applied
"""
import pytest
from pydantic import BaseModel
from totodev_pub.llm.simplified_llm import SimplifiedLLM
from totodev_pub.llm.fake_llm import FakeLLM
import asyncio
from unittest.mock import patch, MagicMock


# Test data models
class TestStruct(BaseModel):
    answer: int


class MathStruct(BaseModel):
    first_number: int
    second_number: int
    sum: int


# Canned responses for testing
CANNED_RESPONSES = {
    "What is 2+2?": '{"answer": 4}',
    "Add 2 and 3": '{"first_number": 2, "second_number": 3, "sum": 5}',
    "Hello": "Hello response",
    "Test prompt": "Test response"
}


@pytest.fixture(autouse=True)
def cleanup_llm_makers():
    """Clean up LLM makers before and after each test"""
    SimplifiedLLM._llm_makers.clear()
    SimplifiedLLM._pkd_maker_params.clear()
    yield
    SimplifiedLLM._llm_makers.clear()
    SimplifiedLLM._pkd_maker_params.clear()


def get_response_func(prompt: str) -> str:
    """Response generator function for test LLM"""
    if prompt.startswith('Human: '):
        prompt = prompt[7:].strip()
    return CANNED_RESPONSES.get(prompt, "--UNHANDLED RESPONSE--")


@pytest.mark.asyncio
async def test_register_llm_maker_with_default_temperature():
    """Test registering an LLM maker with default_temperature"""
    # Register LLM with default temperature
    SimplifiedLLM.register_fake_llm_maker(
        config_name="TEMP_LLM_1.0",
        response_func=get_response_func,
        default_temperature=1.0
    )
    
    # Verify the maker was registered
    assert "TEMP_LLM_1.0" in SimplifiedLLM.available_llm_configs()
    
    # Verify default_temperature is stored
    maker = SimplifiedLLM._llm_makers["TEMP_LLM_1.0"]
    assert maker.default_temperature == 1.0


@pytest.mark.asyncio
async def test_register_llm_maker_without_default_temperature():
    """Test registering an LLM maker without default_temperature (should be None)"""
    SimplifiedLLM.register_fake_llm_maker(
        config_name="NO_TEMP_LLM",
        response_func=get_response_func
    )
    
    maker = SimplifiedLLM._llm_makers["NO_TEMP_LLM"]
    assert maker.default_temperature is None


@pytest.mark.asyncio
async def test_a_answer_str_uses_default_temperature():
    """Test that a_answer_str uses default_temperature when temperature is not explicitly provided"""
    # Register LLM with default temperature
    SimplifiedLLM.register_fake_llm_maker(
        config_name="DEFAULT_TEMP_LLM",
        response_func=get_response_func,
        default_temperature=0.8
    )
    
    # Mock the bind method to verify temperature is applied
    with patch.object(SimplifiedLLM, 'create_llm_and_throttle') as mock_create:
        mock_llm = MagicMock()
        mock_llm.bind = MagicMock(return_value=mock_llm)
        mock_create.return_value = (mock_llm, None)
        
        # Call without explicit temperature
        result = await SimplifiedLLM.a_answer_str(
            "Hello",
            config_name="DEFAULT_TEMP_LLM"
        )
        
        # Verify bind was called with default temperature
        mock_llm.bind.assert_called_once()
        call_kwargs = mock_llm.bind.call_args[1]
        assert 'temperature' in call_kwargs
        assert call_kwargs['temperature'] == 0.8


@pytest.mark.asyncio
async def test_a_answer_str_explicit_temperature_overrides_default():
    """Test that explicit temperature parameter overrides default_temperature"""
    # Register LLM with default temperature
    SimplifiedLLM.register_fake_llm_maker(
        config_name="OVERRIDE_TEMP_LLM",
        response_func=get_response_func,
        default_temperature=0.5
    )
    
    # Mock the bind method to verify explicit temperature is used
    with patch.object(SimplifiedLLM, 'create_llm_and_throttle') as mock_create:
        mock_llm = MagicMock()
        mock_llm.bind = MagicMock(return_value=mock_llm)
        mock_create.return_value = (mock_llm, None)
        
        # Call with explicit temperature
        result = await SimplifiedLLM.a_answer_str(
            "Hello",
            config_name="OVERRIDE_TEMP_LLM",
            temperature=1.0
        )
        
        # Verify bind was called with explicit temperature, not default
        mock_llm.bind.assert_called_once()
        call_kwargs = mock_llm.bind.call_args[1]
        assert call_kwargs['temperature'] == 1.0
        assert call_kwargs['temperature'] != 0.5


@pytest.mark.asyncio
async def test_a_answer_str_no_temperature_when_none():
    """Test that when temperature is None and no default, temperature is not bound"""
    SimplifiedLLM.register_fake_llm_maker(
        config_name="NO_TEMP_LLM",
        response_func=get_response_func
    )
    
    with patch.object(SimplifiedLLM, 'create_llm_and_throttle') as mock_create:
        mock_llm = MagicMock()
        mock_llm.bind = MagicMock(return_value=mock_llm)
        mock_create.return_value = (mock_llm, None)
        
        # Call without temperature and no default
        result = await SimplifiedLLM.a_answer_str(
            "Hello",
            config_name="NO_TEMP_LLM"
        )
        
        # bind should not be called if no temperature parameters
        # (Actually, it might be called for response_format, so we check the call)
        if mock_llm.bind.called:
            call_kwargs = mock_llm.bind.call_args[1]
            assert 'temperature' not in call_kwargs


@pytest.mark.asyncio
async def test_a_answer_struct_uses_default_temperature():
    """Test that a_answer_struct uses default_temperature when temperature is not explicitly provided"""
    SimplifiedLLM.register_fake_llm_maker(
        config_name="STRUCT_TEMP_LLM",
        response_func=get_response_func,
        default_temperature=0.9
    )
    
    with patch.object(SimplifiedLLM, 'create_llm_and_throttle') as mock_create, \
         patch('totodev_pub.llm.simplified_llm.RepairingJsonOutputParser.parse', return_value={"answer": 4}):
        mock_llm = MagicMock()
        mock_llm.bind = MagicMock(return_value=mock_llm)
        mock_create.return_value = (mock_llm, None)
        
        # Call without explicit temperature
        result = await SimplifiedLLM.a_answer_struct(
            "What is 2+2?",
            pydantic_data_class=TestStruct,
            config_name="STRUCT_TEMP_LLM",
            json_mode=True
        )
        
        # Verify bind was called with default temperature
        mock_llm.bind.assert_called()
        # Check if temperature was in any of the bind calls
        bind_calls = [call[1] for call in mock_llm.bind.call_args_list]
        temp_found = any('temperature' in kwargs and kwargs['temperature'] == 0.9 for kwargs in bind_calls)
        assert temp_found, "Default temperature should be bound to LLM"


@pytest.mark.asyncio
async def test_a_answer_struct_explicit_temperature_overrides_default():
    """Test that explicit temperature parameter overrides default_temperature in a_answer_struct"""
    SimplifiedLLM.register_fake_llm_maker(
        config_name="STRUCT_OVERRIDE_LLM",
        response_func=get_response_func,
        default_temperature=0.3
    )
    
    with patch.object(SimplifiedLLM, 'create_llm_and_throttle') as mock_create, \
         patch('totodev_pub.llm.simplified_llm.RepairingJsonOutputParser.parse', return_value={"first_number": 2, "second_number": 3, "sum": 5}):
        mock_llm = MagicMock()
        mock_llm.bind = MagicMock(return_value=mock_llm)
        mock_create.return_value = (mock_llm, None)
        
        # Call with explicit temperature
        result = await SimplifiedLLM.a_answer_struct(
            "Add 2 and 3",
            pydantic_data_class=MathStruct,
            config_name="STRUCT_OVERRIDE_LLM",
            temperature=1.0,
            json_mode=True
        )
        
        # Verify bind was called with explicit temperature
        bind_calls = [call[1] for call in mock_llm.bind.call_args_list]
        temp_found = any('temperature' in kwargs and kwargs['temperature'] == 1.0 for kwargs in bind_calls)
        assert temp_found, "Explicit temperature should override default"


@pytest.mark.asyncio
async def test_register_maker_by_pkd_with_default_temperature():
    """Test registering LLM via PKD config with default_temperature"""
    config = {
        "protocol": "fake",
        "response_func": get_response_func,
        "default_temperature": "0.7"
    }
    
    SimplifiedLLM.register_maker_by_pkd("PKD_TEMP_LLM", config)
    
    # Verify registration
    assert "PKD_TEMP_LLM" in SimplifiedLLM.available_llm_configs()
    
    # Verify default_temperature is stored and converted to float
    maker = SimplifiedLLM._llm_makers["PKD_TEMP_LLM"]
    assert maker.default_temperature == 0.7
    assert isinstance(maker.default_temperature, float)


@pytest.mark.asyncio
async def test_register_maker_by_pkd_with_default_temperature_float():
    """Test registering LLM via PKD config with default_temperature as float"""
    config = {
        "protocol": "fake",
        "response_func": get_response_func,
        "default_temperature": 0.9
    }
    
    SimplifiedLLM.register_maker_by_pkd("PKD_TEMP_FLOAT_LLM", config)
    
    maker = SimplifiedLLM._llm_makers["PKD_TEMP_FLOAT_LLM"]
    assert maker.default_temperature == 0.9


@pytest.mark.asyncio
async def test_temperature_integration_with_real_calls():
    """Integration test: verify temperature works with actual LLM calls"""
    SimplifiedLLM.register_fake_llm_maker(
        config_name="INTEGRATION_LLM",
        response_func=get_response_func,
        default_temperature=0.6
    )
    
    # Make actual calls to verify they work
    str_result = await SimplifiedLLM.a_answer_str(
        "Hello",
        config_name="INTEGRATION_LLM"
    )
    assert str_result == "Hello response"
    
    struct_result = await SimplifiedLLM.a_answer_struct(
        "What is 2+2?",
        pydantic_data_class=TestStruct,
        config_name="INTEGRATION_LLM",
        json_mode=True
    )
    assert isinstance(struct_result, TestStruct)
    assert struct_result.answer == 4


@pytest.mark.asyncio
async def test_temperature_with_explicit_override_integration():
    """Integration test: verify explicit temperature override works"""
    SimplifiedLLM.register_fake_llm_maker(
        config_name="OVERRIDE_INTEGRATION_LLM",
        response_func=get_response_func,
        default_temperature=0.2
    )
    
    # Call with explicit temperature that overrides default
    result = await SimplifiedLLM.a_answer_str(
        "Test prompt",
        config_name="OVERRIDE_INTEGRATION_LLM",
        temperature=0.95
    )
    assert result == "Test response"


@pytest.mark.asyncio
async def test_load_balanced_llm_with_default_temperature():
    """Test that load-balanced LLMs preserve default_temperature"""
    config1 = {
        "protocol": "fake",
        "response_func": get_response_func,
        "default_temperature": 0.7,
        "allow_load_balanced": True
    }
    config2 = {
        "protocol": "fake",
        "response_func": get_response_func,
        "default_temperature": 0.7,
        "allow_load_balanced": True
    }
    
    SimplifiedLLM.register_maker_by_pkd("LB_TEMP_LLM", config1)
    SimplifiedLLM.register_maker_by_pkd("LB_TEMP_LLM", config2)
    
    # Verify load-balanced maker has default_temperature
    maker = SimplifiedLLM._llm_makers["LB_TEMP_LLM"]
    assert hasattr(maker, 'default_temperature')
    assert maker.default_temperature == 0.7


def test_register_llm_maker_direct_with_temperature():
    """Test direct registration with default_temperature"""
    def make_llm():
        return FakeLLM(response_func=get_response_func)
    
    SimplifiedLLM.register_llm_maker(
        config_name="DIRECT_TEMP_LLM",
        model_constructor=make_llm,
        default_temperature=0.85
    )
    
    maker = SimplifiedLLM._llm_makers["DIRECT_TEMP_LLM"]
    assert maker.default_temperature == 0.85

