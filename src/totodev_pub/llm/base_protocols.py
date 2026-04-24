# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Protocol implementations for various LLM types.

This module contains the protocol implementations for different LLM types.
Each protocol is registered using the LLMProtocolRegistry decorator.
"""

from typing import Optional, Callable, Union, Any, Coroutine
from .protocol_registry import LLMProtocolRegistry, DEFAULT_LLM_TIMEOUT, logger
from .fake_llm import FakeLLM
from totodev_pub.optional_dependencies import raise_missing_dependency

@LLMProtocolRegistry.register_protocol('fake')
def create_fake_llm(
    response_func: Callable[[str], Union[str, Coroutine[Any, Any, str]]],
    **kwargs
) -> FakeLLM:
    """Create a fake LLM instance for testing."""
    return FakeLLM(response_func=response_func)

@LLMProtocolRegistry.register_protocol('azure')
def create_azure_llm(
    deployment_name: str,
    api_key: str,
    api_base: str,
    api_version: str,
    **kwargs
) -> Any:
    """Create an Azure OpenAI LLM instance."""
    from langchain_community.chat_models import AzureChatOpenAI
    
    if not api_key or not api_key.strip():
        raise ValueError("api_key cannot be empty or whitespace")
        
    return AzureChatOpenAI(
        deployment_name=deployment_name,
        openai_api_key=api_key,
        azure_endpoint=api_base,
        openai_api_version=api_version,
        timeout=DEFAULT_LLM_TIMEOUT
    )

@LLMProtocolRegistry.register_protocol('openai')
def create_openai_llm(
    model_name: str,
    api_key: str,
    base_url: Optional[str] = None,
    **kwargs
) -> Any:
    """Create an OpenAI LLM instance."""
    from langchain_community.chat_models import ChatOpenAI
    
    if not api_key or not api_key.strip():
        raise ValueError("api_key cannot be empty or whitespace")
        
    return ChatOpenAI(
        base_url=base_url,
        model_name=model_name,
        openai_api_key=api_key,
        timeout=DEFAULT_LLM_TIMEOUT
    )

@LLMProtocolRegistry.register_protocol('huggingface')
def create_huggingface_llm(
    endpoint_url: str,
    api_key: str,
    **kwargs
) -> Any:
    """Create a HuggingFace LLM instance."""
    from langchain_community.llms import HuggingFaceEndpoint
    
    if not api_key or not api_key.strip():
        raise ValueError("api_key cannot be empty or whitespace")
        
    return HuggingFaceEndpoint(
        endpoint_url=endpoint_url,
        huggingfacehub_api_token=api_key,
        task="text-generation",
        timeout=DEFAULT_LLM_TIMEOUT
    )

@LLMProtocolRegistry.register_protocol('google')
def create_google_llm(
    api_key: str,
    model_name: str = "gemini-pro",
    **kwargs
) -> Any:
    """Create a Google Gemini LLM instance."""
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        import google.generativeai as genai
        
        genai.configure(api_key=api_key)
        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            timeout=DEFAULT_LLM_TIMEOUT
        )
    except ImportError:
        logger.debug("Google Generative AI package not available. The 'google' protocol will not be registered.")
        raise_missing_dependency(
            feature="Google Gemini protocol",
            packages=["langchain-google-genai", "google-auth"],
            extra="llm",
        )