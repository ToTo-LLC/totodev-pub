# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Test cases for ToToPipeBase parameter validation functionality."""

import tempfile
from typing import Dict, Any, List, Optional
import pytest
from pydantic import BaseModel

pytest.importorskip("luigi")  # 'pipes' extra; skip when luigi is unavailable

from totodev_pub.pipes.toto_pipe_base import ToToPipeBase
from totodev_pub.pipes.toto_pipe_type_info import ToToPipeTypeInfo
from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.pipes.special_pipe_file_nickname import SpecialPipeFileNickname


class SampleInputModel(BaseModel, FileMappedPydanticMixin):
    """Sample input model for testing."""
    value: str


class SampleOutputModel(BaseModel, FileMappedPydanticMixin):
    """Sample output model for testing."""
    result: str


class SimpleTestPipe(ToToPipeBase):
    """Simple test pipe implementation."""
    @classmethod
    def pipe_type_info(cls) -> ToToPipeTypeInfo:
        return ToToPipeTypeInfo(
            inputs={
                "input": ("input.json", SampleInputModel),
            },
            outputs={
                "output": ("output.json", SampleOutputModel)
            },
            private_cfgs=[]
        )

    def execute(self):
        input_data = self.load_files("input")[0]
        output_data = SampleOutputModel(result=f"Processed: {input_data.value}")
        output_data.save(file_path=str(self.resolve_nickname("output")))


class ValidationTestPipe(SimpleTestPipe):
    """Test pipe that implements parameter validation."""
    @classmethod
    def bind_params_errors(cls, params: Dict[str, Any]) -> List[str]:
        errors = []
        if not params:
            errors.append("Missing required parameter 'batch_size'")
            return errors

        if 'batch_size' not in params:
            errors.append("Missing required parameter 'batch_size'")
        elif not isinstance(params['batch_size'], int):
            errors.append("Parameter 'batch_size' must be an integer")
        elif params['batch_size'] <= 0:
            errors.append("Parameter 'batch_size' must be positive")
        
        if 'mode' in params and params['mode'] not in ['train', 'test']:
            errors.append("Parameter 'mode' must be either 'train' or 'test'")
            
        return errors

    def bind_inputs(self, explicit_inputs: Dict[str, Any], params: Optional[Dict[str, Any]] = None, **kwargs) -> None:
        """Override bind_inputs to validate parameters."""
        if params is not None:
            errors = self.bind_params_errors(params)
            if errors:
                raise ValueError("\n".join(errors))
        super().bind_inputs(explicit_inputs, params=params, **kwargs)


@pytest.fixture
def test_pipe():
    """Create a test pipe instance."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield SimpleTestPipe(working_dir=temp_dir)


def test_base_pipe_params_validation(test_pipe):
    """Test that base pipe accepts any parameters."""
    # Base pipe should accept any parameters
    test_pipe.bind_inputs(
        explicit_inputs={"input": SampleInputModel(value="test")},
        params={"any_param": "any_value"}
    )
    assert test_pipe.resolve_nickname(SpecialPipeFileNickname.BEGIN).exists()


def test_derived_pipe_params_validation_success():
    """Test successful parameter validation in derived pipe."""
    with tempfile.TemporaryDirectory() as temp_dir:
        pipe = ValidationTestPipe(working_dir=temp_dir)
        # Valid parameters should not raise an error
        pipe.bind_inputs(
            explicit_inputs={"input": SampleInputModel(value="test")},
            params={
                "batch_size": 32,
                "mode": "train"
            }
        )
        assert pipe.resolve_nickname(SpecialPipeFileNickname.BEGIN).exists()


def test_derived_pipe_params_validation_failure():
    """Test parameter validation failures in derived pipe."""
    with tempfile.TemporaryDirectory() as temp_dir:
        pipe = ValidationTestPipe(working_dir=temp_dir)
        
        # Test missing required parameter
        with pytest.raises(ValueError) as exc_info:
            pipe.bind_inputs(
                explicit_inputs={"input": SampleInputModel(value="test")},
                params={"mode": "train"}
            )
        assert "Missing required parameter 'batch_size'" in str(exc_info.value)
        
        # Test invalid parameter type
        with pytest.raises(ValueError) as exc_info:
            pipe.bind_inputs(
                explicit_inputs={"input": SampleInputModel(value="test")},
                params={"batch_size": "32"}  # string instead of int
            )
        assert "Parameter 'batch_size' must be an integer" in str(exc_info.value)
        
        # Test invalid parameter value
        with pytest.raises(ValueError) as exc_info:
            pipe.bind_inputs(
                explicit_inputs={"input": SampleInputModel(value="test")},
                params={"batch_size": 0}  # must be positive
            )
        assert "Parameter 'batch_size' must be positive" in str(exc_info.value)
        
        # Test invalid enum value
        with pytest.raises(ValueError) as exc_info:
            pipe.bind_inputs(
                explicit_inputs={"input": SampleInputModel(value="test")},
                params={
                    "batch_size": 32,
                    "mode": "invalid"  # must be 'train' or 'test'
                }
            )
        assert "Parameter 'mode' must be either 'train' or 'test'" in str(exc_info.value)


def test_no_validation_when_params_none(test_pipe):
    """Test that validation is skipped when params is None."""
    # Should not perform any validation when params is None
    test_pipe.bind_inputs(
        explicit_inputs={"input": SampleInputModel(value="test")},
        params=None
    )
    assert test_pipe.resolve_nickname(SpecialPipeFileNickname.BEGIN).exists() 