# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from typing import Dict, Any, Optional, List, Mapping, AsyncGenerator, Generator, Callable, Union, Coroutine
from langchain.callbacks.manager import CallbackManagerForLLMRun
from langchain.llms.base import LLM
from langchain_core.outputs import GenerationChunk
from langchain_core.outputs import Generation, LLMResult
import asyncio
import json


class FakeLLM(LLM):
    """
    A fake LLM implementation for testing that returns canned responses.
    Rather than invoking an LLM, this class uses a lookup provided at construction 
    to return (potentially)canned responses.
    """
    
    response_func: Callable[[str], Union[str, Coroutine[Any, Any, str]]]
    
    @classmethod
    def register_llm(cls, config_name: str, response_generator: Callable[[str], Union[str, Coroutine[Any, Any, str]]], register_func: Optional[Callable] = None) -> None:
        """Register a fake LLM with SimplifiedLLM.
        
        Args:
            config_name: Name to register this LLM under
            response_generator: Function that takes a prompt and returns a response
            register_func: Optional function to use for registration. If not provided, will use SimplifiedLLM.register_fake_llm_maker
        """
        if register_func is None:
            from totodev_pub.llm.simplified_llm import SimplifiedLLM
            register_func = SimplifiedLLM.register_fake_llm_maker
            
        register_func(
            config_name=config_name,
            response_func=response_generator
        )
    
    def __init__(self, response_func: Callable[[str], Union[str, Coroutine[Any, Any, str]]], **kwargs):
        """Initialize the fake LLM.
        
        Args:
            response_func: A callable that takes a prompt string and returns a response string or an async function.
                         To use a dictionary, wrap it in a lambda: lambda p: my_dict.get(p, "default")
            **kwargs: Additional keyword arguments to pass to the parent LLM class
        """
        super().__init__(response_func=response_func, **kwargs)

    def _get_response(self, prompt: str, **kwargs: Any) -> Union[str, Coroutine[Any, Any, str]]:
        """Get response for a prompt."""
        if prompt.startswith('Human: '):
            prompt = prompt[7:].strip() # strip off the "Human: " prefix that might be added by intermediate layers
        return self.response_func(prompt)

    def _call(self, prompt: str,
              stop: Optional[List[str]] = None,
              run_manager: Optional[CallbackManagerForLLMRun] = None,
              **kwargs: Any
             ) -> str:
        """Execute the fake LLM call."""
        response = self._get_response(prompt, **kwargs)
        if asyncio.iscoroutine(response):
            # If it's an async function, run it in an event loop
            loop = asyncio.get_event_loop()
            response = loop.run_until_complete(response)
        return response

    async def _agenerate(self, prompts: List[str],
                         stop: Optional[List[str]] = None,
                         run_manager: Optional[CallbackManagerForLLMRun] = None,
                         **kwargs: Any
                        ) -> LLMResult:
        """Generate async responses."""
        generations = []
        for prompt in prompts:
            response = self._get_response(prompt)
            if asyncio.iscoroutine(response):
                response = await response
            generations.append([Generation(text=response)])
        return LLMResult(generations=generations)

    async def _astream(self, prompt: str,
                       stop: Optional[List[str]] = None,
                       run_manager: Optional[CallbackManagerForLLMRun] = None,
                       **kwargs: Any
                      ) -> AsyncGenerator[GenerationChunk, None]:
        """Stream responses for async calls."""
        response = self._get_response(prompt)
        if asyncio.iscoroutine(response):
            response = await response
        # Simulate streaming by yielding words
        for word in response.split():
            yield GenerationChunk(text=word)
            if stop and any(s in word for s in stop):
                break
            await asyncio.sleep(0.01)  # Small delay between words

    def _stream(self, prompt: str,
                stop: Optional[List[str]] = None,
                run_manager: Optional[CallbackManagerForLLMRun] = None,
                **kwargs: Any
               ) -> Generator[GenerationChunk, None, None]:
        """Stream responses for sync calls."""
        response = self._get_response(prompt)
        # Simulate streaming by yielding words
        for word in response.split():
            yield GenerationChunk(text=word)

    @property
    def _llm_type(self) -> str:
        """Return identifier for this LLM."""
        return "fake_llm"

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {"fake_llm": True} 