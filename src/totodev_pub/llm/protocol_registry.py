# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from typing import Optional, Callable, Dict, List, Any, Tuple, Set, TypeVar
from inspect import signature, Parameter
from functools import wraps
from totodev_pub.logger import MyLogger

logger = MyLogger.shared_logger()

DEFAULT_LLM_TIMEOUT = 90  # seconds.  Arbitrary

# Type for protocol maker functions
T = TypeVar('T', bound='BaseLLM')  # Forward reference since we can't import BaseLLM here
ProtocolMakerFunc = Callable[..., T]

class LLMProtocolRegistry:
    """Registry for LLM protocols and their maker functions.
    
    The LLMProtocolRegistry provides a centralized way to register and manage different LLM protocols.
    Each protocol represents a specific way to create and configure an LLM instance (e.g., Azure, OpenAI, HuggingFace).
    
    To add a new protocol:
    1. Create a function that takes the necessary parameters to create your LLM instance
    2. Decorate it with @LLMProtocolRegistry.register_protocol('your_protocol_name')
    3. Place the function in base_protocols.py or your own protocol module
    """
    
    _protocols: Dict[str, Tuple[ProtocolMakerFunc, Set[str], Set[str]]] = {}
    
    @classmethod
    def register_protocol(cls, 
                         protocol: str, 
                         required_args: Optional[List[str]] = None,
                         optional_args: Optional[List[str]] = None) -> Callable[[ProtocolMakerFunc], ProtocolMakerFunc]:
        """Decorator to register a protocol maker function.
        
        If required_args and optional_args are not provided, they will be inferred from the function signature.
        Args with default values are considered optional, those without are required.
        
        Args:
            protocol: Name of the protocol (e.g., 'azure', 'openai', 'fake')
            required_args: List of required argument names (optional)
            optional_args: List of optional argument names (optional)
        """
        def decorator(maker_func: ProtocolMakerFunc) -> ProtocolMakerFunc:
            sig = signature(maker_func)
            
            # If args not provided, infer from function signature
            if required_args is None or optional_args is None:
                inferred_required = set()
                inferred_optional = set()
                
                for name, param in sig.parameters.items():
                    # Skip self/cls for methods
                    if name in ('self', 'cls'):
                        continue
                    # Parameters with default values are optional
                    if param.default == Parameter.empty and param.kind != Parameter.VAR_KEYWORD:
                        inferred_required.add(name)
                    else:
                        inferred_optional.add(name)
            
            final_required = set(required_args if required_args is not None else inferred_required)
            final_optional = set(optional_args if optional_args is not None else inferred_optional)
            
            # Register the protocol
            cls._protocols[protocol.lower()] = (maker_func, final_required, final_optional)
            #logger.debug(f"Registered protocol '{protocol}' with maker function {maker_func.__name__}")
            
            @wraps(maker_func)
            def wrapper(*args, **kwargs):
                return maker_func(*args, **kwargs)
            
            return wrapper
        return decorator
    
    @classmethod
    def get_protocol(cls, protocol: str) -> Tuple[ProtocolMakerFunc, Set[str], Set[str]]:
        """Get the maker function and argument sets for a protocol."""
        protocol = protocol.lower()
        if protocol not in cls._protocols:
            supported = list(cls._protocols.keys())
            raise ValueError(f"Unknown protocol '{protocol}'. Must be one of: {', '.join(supported)}")
        return cls._protocols[protocol]
    
    @classmethod
    def list_protocols(cls) -> List[str]:
        """List all registered protocols."""
        return list(cls._protocols.keys()) 