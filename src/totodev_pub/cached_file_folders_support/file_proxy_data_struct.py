# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Serializable data proxy implementation for CachedFileFolders.
"""

from typing import Union, Optional, Dict, Any
from pathlib import Path
import os
import shutil
import json
import yaml
import tempfile
import asyncio
from enum import Enum
from pydantic import BaseModel

from .file_proxy_base import FileProxyBase


class SerializableDataProxy(FileProxyBase):
    """
    Proxy for serializable data structures that get materialized to JSON or YAML files.
    
    This implementation ensures deterministic serialization by sorting dictionary keys,
    which allows simple file comparison tools (hashing, diff, etc.) to reliably detect
    changes in the data rather than just key ordering differences.
    """
    
    @staticmethod
    def _convert_enums_to_strings(data):
        """
        Recursively convert enum objects to their string values for serialization.
        
        Args:
            data: The data structure to process
            
        Returns:
            The data structure with enums converted to strings
        """
        if isinstance(data, Enum):
            return data.value
        elif isinstance(data, dict):
            return {key: SerializableDataProxy._convert_enums_to_strings(value) for key, value in data.items()}
        elif isinstance(data, list):
            return [SerializableDataProxy._convert_enums_to_strings(item) for item in data]
        elif isinstance(data, tuple):
            return tuple(SerializableDataProxy._convert_enums_to_strings(item) for item in data)
        else:
            return data
    
    @staticmethod
    def _make_dict_deterministic(data: dict) -> dict:
        """
        Recursively sort dictionary keys to ensure deterministic serialization.
        
        DESIGN NOTE: Deterministic serialization is strongly encouraged for all
        FileProxyBase implementations. When serialized output is deterministic,
        simple file comparison tools (hash comparison, diff utilities, version control)
        can reliably detect actual content changes rather than being confused by
        non-semantic differences like key ordering.
        
        This is one example approach using sorted keys for dict objects. Other valid
        approaches include:
        - Using OrderedDict with controlled insertion order
        - Custom JSON encoders with sort_keys=True
        - Pydantic models with deterministic field ordering
        - Schema-based serialization libraries
        
        Args:
            data: Dictionary to make deterministic
            
        Returns:
            Dictionary with recursively sorted keys
            
        Note:
            This function only applies to raw dict objects. BaseModel objects
            maintain their own field ordering through Pydantic's serialization.
        """
        if isinstance(data, dict):
            # Sort keys and recursively process values
            return {key: SerializableDataProxy._make_dict_deterministic(value) 
                    for key, value in sorted(data.items())}
        elif isinstance(data, list):
            # Recursively process list items
            return [SerializableDataProxy._make_dict_deterministic(item) for item in data]
        else:
            # Return primitive types and other objects as-is
            return data
    
    def __init__(self, data: Union[BaseModel, Dict[str, Any], list], ref_path: str):
        """
        Initialize a serializable data proxy.
        
        Args:
            data: The data to serialize (BaseModel, dict, or list)
            ref_path: Reference path for the file (must end with .json, .yaml, or .yml)
        """
        self._data = data
        self._ref_path = ref_path
        self._temp_file_path: Optional[str] = None
        self._was_deployed = False
        ref_lower = ref_path.lower()
        if not (ref_lower.endswith('.json') or ref_lower.endswith('.yaml') or ref_lower.endswith('.yml')):
            raise ValueError("ref_path must have .json, .yaml, or .yml extension")

    def ref_path(self) -> str:
        return self._ref_path

    def deploy(self, target_dir: str) -> None:
        if self._was_deployed:
            raise RuntimeError("File has already been deployed")
        if target_dir == "/dev/null":
            if self._temp_file_path and os.path.exists(self._temp_file_path):
                os.remove(self._temp_file_path)
            self._was_deployed = True
            return
        if not os.path.isdir(target_dir):
            raise FileNotFoundError(f"Target directory does not exist: {target_dir}")
        if self._temp_file_path is None:
            raise RuntimeError("File must be materialized before deployment")
        filename = os.path.basename(self._ref_path)
        target_path = os.path.join(target_dir, filename)
        shutil.copy2(self._temp_file_path, target_path)
        self._was_deployed = True
        if os.path.exists(self._temp_file_path):
            os.remove(self._temp_file_path)

    def looks_same(self, other_fpath: str) -> Optional[bool]:
        try:
            if self._temp_file_path is None:
                # Check if we're in an async context (event loop is running)
                try:
                    asyncio.get_running_loop()
                    # Event loop is running - can't use asyncio.run()
                    # Return None to indicate we can't determine (caller should materialize first)
                    return None
                except RuntimeError:
                    # No event loop running - safe to use asyncio.run()
                    asyncio.run(self.materialize(0.0))
            if self._temp_file_path is None or not os.path.exists(self._temp_file_path):
                return None
            our_stat = os.stat(self._temp_file_path)
            other_stat = os.stat(other_fpath)
            if our_stat.st_size != other_stat.st_size:
                return False
            with open(self._temp_file_path, 'r', encoding='utf-8') as f1:
                with open(other_fpath, 'r', encoding='utf-8') as f2:
                    return f1.read() == f2.read()
        except (OSError, IOError):
            return None

    async def materialize(self, blocking_secs: float, temp_dir: Optional[Path] = None) -> bool:
        if self._temp_file_path is not None:
            return True
        ref_lower = self._ref_path.lower()
        if ref_lower.endswith('.json'):
            suffix = '.json'
            mode = 'w'
            encoding = 'utf-8'
        elif ref_lower.endswith('.yaml') or ref_lower.endswith('.yml'):
            suffix = '.yaml'
            mode = 'w'
            encoding = 'utf-8'
        else:
            raise ValueError("Unsupported file extension")
        
        # Use provided temp_dir if available, otherwise use system temp
        if temp_dir:
            temp_fd, self._temp_file_path = tempfile.mkstemp(dir=temp_dir, suffix=suffix)
        else:
            temp_fd, self._temp_file_path = tempfile.mkstemp(suffix=suffix)
        
        try:
            with os.fdopen(temp_fd, mode, encoding=encoding) as f:
                if ref_lower.endswith('.json'):
                    if isinstance(self._data, dict):
                        # Dict gets deterministic treatment
                        deterministic_data = self._make_dict_deterministic(self._data)
                        serializable_data = self._convert_enums_to_strings(deterministic_data)
                        json.dump(serializable_data, f, indent=2)
                    else:
                        # Everything else uses default serialization
                        if isinstance(self._data, BaseModel):
                            data_dict = self._data.model_dump()
                            serializable_data = self._convert_enums_to_strings(data_dict)
                            json.dump(serializable_data, f, indent=2)
                        else:
                            serializable_data = self._convert_enums_to_strings(self._data)
                            json.dump(serializable_data, f, indent=2)
                else:
                    if isinstance(self._data, dict):
                        # Dict gets deterministic treatment
                        deterministic_data = self._make_dict_deterministic(self._data)
                        serializable_data = self._convert_enums_to_strings(deterministic_data)
                        yaml.dump(serializable_data, f, default_flow_style=False)
                    else:
                        # Everything else uses default serialization
                        if isinstance(self._data, BaseModel):
                            data_dict = self._data.model_dump()
                            serializable_data = self._convert_enums_to_strings(data_dict)
                            yaml.dump(serializable_data, f, default_flow_style=False)
                        else:
                            serializable_data = self._convert_enums_to_strings(self._data)
                            yaml.dump(serializable_data, f, default_flow_style=False)
            return True
        except Exception:
            if os.path.exists(self._temp_file_path):
                os.remove(self._temp_file_path)
            self._temp_file_path = None
            raise

    def get_context_info(self) -> Dict[str, Any]:
        # Get data type information without exposing the actual data
        data_type = type(self._data).__name__
        if isinstance(self._data, (dict, list)):
            data_size = len(self._data)
        else:
            data_size = "unknown"
        
        return {
            "proxy_type": "SerializableDataProxy",
            "ref_path": self._ref_path,
            "data_type": data_type,
            "data_size": data_size,
            "temp_file_path": self._temp_file_path,
            "was_deployed": self._was_deployed
        }

    def __del__(self):
        if self._temp_file_path and os.path.exists(self._temp_file_path):
            try:
                os.remove(self._temp_file_path)
            except OSError:
                pass
