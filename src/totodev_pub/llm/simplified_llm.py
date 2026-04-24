# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from pydantic import BaseModel
import os
import json
import asyncio
import yaml
import time
import re
import warnings
from totodev_pub.dbjig import DbJig
from datetime import date,timedelta
import hashlib
import zlib
import nest_asyncio
from dataclasses import dataclass   #,field
from json_repair import repair_json
from typing import Optional, Callable, Dict, List, Any, Tuple,Union,Set,Iterable,Literal,Awaitable,Coroutine, TypeVar
from langchain.prompts import PromptTemplate, ChatPromptTemplate 
from langchain.schema import AIMessage
from jinja2 import Template as JinjaTemplate
from langchain_core.output_parsers.json import JsonOutputParser
from langchain_community.llms import BaseLLM
from langchain.schema import OutputParserException, SystemMessage, HumanMessage
from langchain_community.chat_models import ChatOpenAI, AzureChatOpenAI
from langchain_community.llms import HuggingFaceEndpoint
from langchain_google_genai import ChatGoogleGenerativeAI  # For Gemini support
from langchain_core.outputs import Generation
from langchain_core.language_models import BaseChatModel
from inspect import signature, Parameter
from functools import wraps
import logging
from .protocol_registry import LLMProtocolRegistry, DEFAULT_LLM_TIMEOUT, logger
from .base_protocols import *
from .fake_llm import FakeLLM  # Add this import

from totodev_pub.logger import MyLogger
from totodev_pub.llm.self_throttle import SelfBandwidthThrottle
import random

DEFAULT_CACHED_PROMPT_EXPIRATION_MINUTES = 30*24*60  # 30 days

nest_asyncio.apply()

# Type for protocol maker functions
T = TypeVar('T', bound=BaseLLM)
ProtocolMakerFunc = Callable[..., T]

class RepairingJsonOutputParser(JsonOutputParser):
    """Wrapper class to add an attempt at repairing slightly flawed JSON.
    
    """
    def parse(self, text):
        try:
            v = super().parse(text)
            if not isinstance(v,dict):
                raise OutputParserException(f"Expected a dictionary, but got {type(v)}")
            return v
        except Exception as e:
            # Attempt to repair the JSON
            try:
                if text.startswith('{') and not text.endswith('}') and len(text) > 1000:
                    text = text + '"}'  # crude attempt to fix missing closing brace/quote on long JSON strings
                repaired_json = repair_json(text)
            except Exception as e_ignore:
                logger.warning("Failed to repair JSON reply from LLM")
                raise e
            if (retried_parse := super().parse(repaired_json)):
                return retried_parse
            raise ValueError(f"Input text failed to parse and we were unable to repair it: [{text[:60]}]")


    def parse_result(self, result: list[Generation], *, partial: bool = False) -> Any:
        """Thin wrapper around parent class method to add a repair attempt."""
        try:
            return super().parse_result(result, partial=partial)
        except OutputParserException as e:
            # Attempt to repair the JSON
            text = result[0].text
            text = text.strip()
            try:
                repaired_json_str = repair_json(text)
            except Exception as e_ignore:
                logger.warning("Failed to repair JSON reply from LLM")
                raise e  # raise the old exception

            return super().parse_result([Generation(text=repaired_json_str)], partial=partial)
    

class _LLMInferenceCache:
    """
    Used internally by SimplifiedLLM, this class provides a simple cache of LLM inferences with expiration.

    The cache is a simple key-value store in a SQLite database.
    Key is the prompt.
    The value is the LLM response.
    The cache is expired after the specified number of minutes.

    Optionally can compress strings during storage and decompression during retrieval at a small performance cost.
    """

    _DDL_SQL = {
                    "00-000-00.sql": """
                    CREATE TABLE IF NOT EXISTS llm_inference_cache (
                        prompt_hash INTEGER,
                        prompt TEXT,
                        response TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (prompt_hash,prompt)
                    );

                    -- insert_entry
                    INSERT INTO llm_inference_cache (prompt_hash,prompt,response) 
                    VALUES (:prompt_hash,:prompt,:response);

                    -- find_entry
                    -- Creates a pquery to select unexpired cache entries if available.
                    SELECT response 
                    FROM llm_inference_cache 
                    WHERE prompt_hash = :prompt_hash    
                          AND prompt = :prompt
                          AND created_at > datetime('now', '-' || :expiration_seconds || ' seconds')
                    LIMIT 1;

                    -- purge_expired
                    DELETE FROM llm_inference_cache 
                    WHERE created_at < datetime('now', '-' || :expiration_seconds || ' seconds');


                    -- purge_entry
                    DELETE FROM llm_inference_cache 
                    WHERE :prompt_hash IS NULL OR prompt_hash = :prompt_hash;
                    """
                }

    def __init__(self, cache_file: str, cache_minutes: Optional[int] = 30, cache_seconds: Optional[float] = None, compress_strings: bool = True):
        """Initialize cache with expiration time in either minutes or seconds"""
        self.cache_file = cache_file
        self.compress_strings = compress_strings
        self.last_purge = date.today() - timedelta(days=1)  # Force purge on first run
        
        if cache_seconds is not None and cache_minutes != 30:  # Allow default cache_minutes
            raise ValueError("Specify either cache_minutes or cache_seconds, not both")
            
        if cache_seconds is not None:
            self.expiration_seconds = cache_seconds
            self.cache_minutes = int(cache_seconds / 60)
        else:
            self.cache_minutes = cache_minutes
            self.expiration_seconds = cache_minutes * 60

        # Initialize database connection
        self.dbj = DbJig(db_file=cache_file, loadsources=self._DDL_SQL)

    def _is_expired(self, timestamp: float) -> bool:
        """Check if a cache entry has expired"""
        return time.time() - timestamp > self.expiration_seconds

    def _periodic_purge(self,force_purge:bool = False):
        """
        Database files can get bloated over time from inserting and deleting expired entries.
        This method will delete old entries from the database file to keep it clean but only do once per day at most.
        """
        if force_purge or self.last_purge < date.today():
            self.dbj.pquery("purge_expired",{"expiration_seconds": int(self.expiration_seconds)})
            self.dbj.query("VACUUM;")
            self.last_purge = date.today()


    def store_entry(self,prompt:str,response:str):
        """
        Store an entry in the cache.
        """
        self._periodic_purge()  
        # Calculate hash before any compression
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        
        if self.compress_strings:
            prompt = zlib.compress(prompt.encode())
            response = zlib.compress(response.encode())
        
        self.dbj.pquery("insert_entry",{
            "prompt_hash": prompt_hash,
            "prompt": prompt,
            "response": response
        })

    def get_entry(self,prompt:str) -> Optional[str]:
        """
        Get an entry from the cache. May return None if the entry is not found or expired.
        """
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        
        if self.compress_strings:
            prompt = zlib.compress(prompt.encode())
            
        result:List[Tuple[str]] = self.dbj.pquery("find_entry",{
            "prompt_hash": prompt_hash,
            "prompt": prompt,
            "expiration_seconds": int(self.expiration_seconds)
        })
        
        if not result:
            return None
        return zlib.decompress(result[0][0]).decode() if self.compress_strings else result[0][0]

    def purge(self,prompt:Optional[str] = None):
        """
        Purge an entry or all entries from the cache.
        If prompt is None, all entries will be purged.
        If prompt is not None, only the entry for the given prompt will be purged.  
        """

        self.dbj.pquery("purge_entry",{ "prompt_hash": None if prompt is None else hashlib.sha256(prompt.encode()).hexdigest() })


@dataclass
class _LLMMaker:
    config_name: Optional[str]
    model_constructor: Callable[[], BaseLLM] 
    input_throttle: SelfBandwidthThrottle
    cache: Optional[_LLMInferenceCache] = None
    default_temperature: Optional[float] = None


class _LoadBalancedLLMMaker(_LLMMaker):
    """A load balancer that distributes requests across multiple LLM makers."""
    def __init__(self, makers: List[_LLMMaker]):
        if not makers:
            raise ValueError("Cannot create a load balancer with no makers")
        self.makers = makers
        # Use the first maker's config name, throttle, cache, and default_temperature as the "primary"
        super().__init__(
            config_name=makers[0].config_name,
            model_constructor=self._load_balanced_constructor,
            input_throttle=makers[0].input_throttle,
            cache=makers[0].cache,
            default_temperature=makers[0].default_temperature
        )

    def _load_balanced_constructor(self) -> BaseLLM:
        """Randomly select a maker and return its LLM instance."""
        chosen_maker = random.choice(self.makers)
        return chosen_maker.model_constructor()


class _LLMRequestLogger:
    """
    Used internally by SimplifiedLLM, this class provides a simple log of LLM requests.
    For use in identifying bandwidth issues.
    
    This class logs LLM requests to a CSV file with the following columns:
    - timestamp: ISO format timestamp of when the request was made
    - logging_label: User-provided label for the request
    - config_name: Name of the LLM configuration used
    - char_count: Number of characters in the request
    - duration_seconds: Time taken to complete the request
    
    Features:
    - Uses file locking via portalocker to handle concurrent writes
    - Implements random backoff and retry when file is locked
    - Queues failed writes in memory for later retry
    - Can be disabled by passing None as log_file
    
    If the log file doesn't exist or is empty, a header row will be written first.
    """
    
    # CSV header fields
    HEADER = ["timestamp", "logging_label", "config_name", "char_count", "duration_seconds"]
    
    def __init__(self, log_file: str, allow_queued_writes: int = 10):
        """
        Initialize the LLM request log.
        
        Args:
            log_file: Path to the log file. If None, logging will be disabled.
            allow_queued_writes: Maximum number of queued writes to allow before raising an exception.
                                Default is 10.
        """
        self.log_file = log_file
        self.allow_queued_writes = allow_queued_writes
        self.queued_requests = []
    
    def _write_to_log(self, entries_to_write: list, max_attempts: int = 2, timeout: float = 0.1, 
                     backoff_strategy: str = "random", backoff_min: float = 0.001, backoff_max: float = 0.05) -> bool:
        """
        Common implementation for writing entries to the log file.
        
        Args:
            entries_to_write: List of entries to write to the log file
            max_attempts: Maximum number of attempts to write to the log file
            timeout: Timeout for acquiring the lock in seconds
            backoff_strategy: Strategy for backoff between attempts ("random" or "exponential")
            backoff_min: Minimum backoff time in seconds
            backoff_max: Maximum backoff time in seconds for random strategy
            
        Returns:
            bool: True if writing was successful, False otherwise
        """
        if self.log_file is None:
            return True  # No-op if logging is disabled
        
        import datetime
        import csv
        import os
        import portalocker
        import random
        import time
        
        # Try to write to the file with locking
        file_exists = os.path.exists(self.log_file)
        file_empty = not file_exists or os.path.getsize(self.log_file) == 0
        
        for attempt in range(max_attempts):
            try:
                # Open the file with locking
                lock_flags = portalocker.LOCK_EX | portalocker.LOCK_NB  # Exclusive, non-blocking lock
                
                # Create parent directories if they don't exist
                if not file_exists:
                    os.makedirs(os.path.dirname(os.path.abspath(self.log_file)), exist_ok=True)
                
                with portalocker.Lock(self.log_file, mode='a+', flags=lock_flags, timeout=timeout) as f:
                    writer = csv.writer(f)
                    
                    # Write header if file is empty
                    if file_empty:
                        writer.writerow(self.HEADER)
                    
                    # Write all entries
                    writer.writerows(entries_to_write)
                
                # If we get here, writing was successful
                return True
                
            except portalocker.LockException:
                # File is locked, implement backoff
                if attempt < max_attempts - 1:  # Only backoff if we have another retry
                    if backoff_strategy == "random":
                        backoff_time = random.uniform(backoff_min, backoff_max)
                    else:  # exponential
                        backoff_time = random.uniform(backoff_min, backoff_max) * (2 ** attempt)
                    time.sleep(backoff_time)
        
        # If we get here, all write attempts failed
        return False
    
    def log_request(self, logging_label: str, config_name: str, char_count: int, duration_seconds: float) -> None:
        """
        Log a request to the LLM.
        
        Args:
            logging_label: User-provided label for the request
            config_name: Name of the LLM configuration used
            char_count: Number of characters in the request
            duration_seconds: Time taken to complete the request
            
        Raises:
            Exception: If the number of queued writes exceeds allow_queued_writes
        """
        if self.log_file is None:
            return  # No-op if logging is disabled
        
        import datetime
        
        # Create a new log entry
        timestamp = datetime.datetime.now().isoformat()
        log_entry = [timestamp, logging_label, config_name, char_count, duration_seconds]
        
        # Add any queued entries to the list to write
        entries_to_write = self.queued_requests + [log_entry]
        
        # Try to write to the log file
        success = self._write_to_log(
            entries_to_write, 
            max_attempts=2, 
            timeout=0.1,
            backoff_strategy="random", 
            backoff_min=0.001, 
            backoff_max=0.05
        )
        
        if success:
            # Writing was successful
            self.queued_requests = []
            return
        
        # If we get here, all write attempts failed
        self.queued_requests = entries_to_write
        
        # Check if we've exceeded the queue limit
        if len(self.queued_requests) > self.allow_queued_writes:
            raise Exception(
                f"LLM request log queue exceeded maximum size of {self.allow_queued_writes}. "
                f"Unable to write to log file '{self.log_file}' after multiple attempts."
            )
    
    def flush(self, max_attempts: int = 5) -> None:
        """
        Attempts to write any queued requests to the log file.
        
        This method makes multiple attempts to write all queued requests to the log file.
        If the queue is not empty after all attempts, an exception is raised.
        
        Args:
            max_attempts: Maximum number of attempts to write to the log file.
                         Default is 5.
                         
        Raises:
            Exception: If the queue is not empty after all flush attempts.
        """
        if self.log_file is None or not self.queued_requests:
            return  # No-op if logging is disabled or queue is empty
        
        # Try to write to the log file with more attempts and exponential backoff
        success = self._write_to_log(self.queued_requests,
                                     max_attempts=max_attempts,
                                     timeout=0.5,
                                     backoff_strategy="exponential",
                                     backoff_min=0.1,
                                     backoff_max=0.5
                                    )
        
        if success:
            # Writing was successful
            self.queued_requests = []
            return
        
        # If we get here, all flush attempts failed
        if self.queued_requests:
            raise Exception(
                f"Failed to flush {len(self.queued_requests)} queued log entries to '{self.log_file}' "
                f"after {max_attempts} attempts. The log file may be locked by another process."
            )


class SimplifiedLLM:
    """A utility class to simplify and consolidate several simple and common LLM operations.  Examples include creating LLMs and sending a single completion to the LLM.
    
    IMPORTANT: You must call register_llm_maker() before using any of the other methods.  This is typically done at startup some time.
    
    IMPORTANT: If you want to use shared throttles, you must set the shared_throttle_filepath parameter in register_llm_maker().
    """

    _llm_makers: Dict[str,_LLMMaker] = {}
    _pkd_maker_params: Dict[str, Dict[str, Any]] = {}  # Store the original PKD parameters for each maker config
    _request_logger: Optional[_LLMRequestLogger] = None # disabled by default (since filename is none)
    _protocols_imported: bool = False
    
    DEFAULT_MODEL_KEY = "DEFAULT"  # can also use None
    DEFAULT_INPUT_THROTLING_CHARS_PER_MIN = 20000*4    # arbitrary at roughly 20k tokens per minute

    # Known numeric parameters that should be coerced to numbers
    _NUMERIC_PARAMS = {
        'chars_per_min_throttle': int,
        'max_requests_per_min': int,
        'timeout': float,
        'default_temperature': float
    }

    def __init__(self) -> None:
        raise NotImplementedError("Do not instantiate!  Use the class methods.")

    @classmethod
    def _ensure_protocols_imported(cls) -> None:
        """Ensure protocols are imported if not already done."""
        if not cls._protocols_imported:
            cls.import_protocols()
            cls._protocols_imported = True

    @staticmethod
    def _to_snake_case(name: str) -> str:
        """Convert a string to snake case (e.g. camelCase -> camel_case)."""
        import re
        name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', name).lower()

    @classmethod
    def _coerce_numeric_params(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        """Coerce known numeric parameters to their proper type."""
        result = params.copy()
        for param_name, param_type in cls._NUMERIC_PARAMS.items():
            if param_name in result and not isinstance(result[param_name], (type(None), param_type)):
                try:
                    result[param_name] = param_type(result[param_name])
                except (ValueError, TypeError):
                    raise ValueError(f"Parameter '{param_name}' must be convertible to {param_type.__name__}")
        return result

    @classmethod
    def _get_supported_protocols(cls) -> List[str]:
        """Get list of supported protocols."""
        return LLMProtocolRegistry.list_protocols()

    @classmethod
    def import_protocols(cls, module_name: str = "totodev_pub.llm.base_protocols") -> None:
        """Import protocol implementations from a module.
        
        This method is used to load protocol implementations into the registry.
        By default, it loads the standard protocols from totodev_pub.llm.base_protocols.
        
        Args:
            module_name: The module to import protocols from. You can specify your own module
                       to add custom protocols.
        """
        import importlib
        try:
            importlib.import_module(module_name)
        except ImportError as e:
            logger.warning(f"Failed to import protocols from {module_name}: {e}")

    @classmethod
    def register_llm_maker(  cls,config_name:Optional[str], 
                           model_constructor: Callable[[],BaseLLM],
                           chars_per_min_throttle: Optional[int] = None,
                           max_requests_per_min: Optional[int] = None,
                           shared_throttle_filepath: Optional[str] = None,
                           allow_load_balanced: bool = False,
                           default_temperature: Optional[float] = None
                          ) -> None:
        """
        Primary and most granular way to set up SimplifiedLLM to be called later.  
        Registers a callable that creates an LLM.  The callable should take no arguments and return an LLM.  
        If the config name is None, the LLM will be the default LLM.  
        If the config name is not None, the LLM will be available by that name.

        Args:
            config_name: Name to register this LLM under, or None for default
            model_constructor: Callable that creates and returns the LLM
            chars_per_min_throttle: Optional character limit per minute for throttling
            max_requests_per_min: Optional maximum number of requests per minute for throttling
            shared_throttle_filepath: Optional file path to enable cross-process throttling
            allow_load_balanced: If True, allows multiple registrations with the same config_name to create a load-balanced pool.
                               If False, raises an error on duplicate config names.
                               Note: This parameter is only used when registering through register_maker_by_pkd().
            default_temperature: Optional default temperature to use when temperature is not explicitly provided in calls
        """
        if max_requests_per_min is not None and chars_per_min_throttle is None:
            raise ValueError("Cannot specify max_requests_per_min without specifying chars_per_min_throttle.")
        throttle = None if chars_per_min_throttle is None else SelfBandwidthThrottle(
                                                                    char_limit=chars_per_min_throttle,
                                                                    interval_secs=60,  # per minute
                                                                    cross_process_logfile=shared_throttle_filepath,
                                                                    max_requests=max_requests_per_min
                                                                )
        config_name = config_name or cls.DEFAULT_MODEL_KEY
        new_maker = _LLMMaker(config_name=config_name, model_constructor=model_constructor, input_throttle=throttle, default_temperature=default_temperature)

        if config_name in cls._llm_makers:
            raise ValueError(f"Config name '{config_name}' already exists.")
            
        cls._llm_makers[config_name] = new_maker

    # NOTE: The following register_*_llm_maker methods are deprecated as of version 2.0.0
    # and scheduled for removal in version 3.0.0. Consider removing after 2025-07-01.
    # Users should migrate to register_maker_by_pkd() instead.
    
    @classmethod
    def register_azure_llm_maker(cls, 
                           config_name: str,
                           deployment_name: str,
                           api_key: str,
                           api_base: str,
                           api_version: str,
                           chars_per_min_throttle: Optional[int] = None,
                           max_requests_per_min: Optional[int] = None,
                           shared_throttle_filepath: Optional[str] = None
                          ) -> None:
        """Register an Azure OpenAI LLM.
        
        .. deprecated:: 2.0.0
           This method is maintained for backward compatibility only and will be removed in version 3.0.0.
           Use `register_maker_by_pkd()` with protocol="azure" instead:
           
           Example:
           ```python
           SimplifiedLLM.register_maker_by_pkd("my_azure", {
               "protocol": "azure",
               "deployment_name": deployment_name,
               "api_key": api_key,
               "api_base": api_base,
               "api_version": api_version
           })
           ```
        """
        warnings.warn(
            "register_azure_llm_maker is deprecated and will be removed in version 3.0.0. "
            "Use register_maker_by_pkd with protocol='azure' instead.",
            DeprecationWarning,
            stacklevel=2
        )
        cls.import_protocols()  # Ensure protocols are loaded
        cls.register_maker_by_pkd(config_name, {
            "protocol": "azure",
            "deployment_name": deployment_name,
            "api_key": api_key,
            "api_base": api_base,
            "api_version": api_version,
            "chars_per_min_throttle": chars_per_min_throttle,
            "max_requests_per_min": max_requests_per_min,
            "shared_throttle_filepath": shared_throttle_filepath
        })

    @classmethod
    def register_openai_llm_maker(cls,
                           config_name: str,
                           model_name: str,
                           api_key: str,
                           base_url: Optional[str] = None,
                           chars_per_min_throttle: Optional[int] = None,
                           max_requests_per_min: Optional[int] = None,
                           shared_throttle_filepath: Optional[str] = None
                          ) -> None:
        """Register an OpenAI LLM.
        
        .. deprecated:: 2.0.0
           This method is maintained for backward compatibility only and will be removed in version 3.0.0.
           Use `register_maker_by_pkd()` with protocol="openai" instead:
           
           Example:
           ```python
           SimplifiedLLM.register_maker_by_pkd("my_openai", {
               "protocol": "openai",
               "model_name": model_name,
               "api_key": api_key,
               "base_url": base_url
           })
           ```
        """
        warnings.warn(
            "register_openai_llm_maker is deprecated and will be removed in version 3.0.0. "
            "Use register_maker_by_pkd with protocol='openai' instead.",
            DeprecationWarning,
            stacklevel=2
        )
        cls.import_protocols()  # Ensure protocols are loaded
        cls.register_maker_by_pkd(config_name, {
            "protocol": "openai",
            "model_name": model_name,
            "api_key": api_key,
            "base_url": base_url,
            "chars_per_min_throttle": chars_per_min_throttle,
            "max_requests_per_min": max_requests_per_min,
            "shared_throttle_filepath": shared_throttle_filepath
        })

    @classmethod
    def register_huggingface_llm_maker(cls,
                                config_name: str,
                                endpoint_url: str,
                                api_key: str,
                                chars_per_min_throttle: Optional[int] = None,
                                max_requests_per_min: Optional[int] = None,
                                shared_throttle_filepath: Optional[str] = None
                               ) -> None:
        """Register a HuggingFace LLM.
        
        .. deprecated:: 2.0.0
           This method is maintained for backward compatibility only and will be removed in version 3.0.0.
           Use `register_maker_by_pkd()` with protocol="huggingface" instead:
           
           Example:
           ```python
           SimplifiedLLM.register_maker_by_pkd("my_hf", {
               "protocol": "huggingface",
               "endpoint_url": endpoint_url,
               "api_key": api_key
           })
           ```
        """
        warnings.warn(
            "register_huggingface_llm_maker is deprecated and will be removed in version 3.0.0. "
            "Use register_maker_by_pkd with protocol='huggingface' instead.",
            DeprecationWarning,
            stacklevel=2
        )
        cls.import_protocols()  # Ensure protocols are loaded
        cls.register_maker_by_pkd(config_name, {
            "protocol": "huggingface",
            "endpoint_url": endpoint_url,
            "api_key": api_key,
            "chars_per_min_throttle": chars_per_min_throttle,
            "max_requests_per_min": max_requests_per_min,
            "shared_throttle_filepath": shared_throttle_filepath
        })

    @classmethod
    def register_google_llm_maker(cls,
                             config_name: str,
                             api_key: str,
                             model_name: str = "gemini-pro",
                             chars_per_min_throttle: Optional[int] = None,
                             max_requests_per_min: Optional[int] = None,
                             shared_throttle_filepath: Optional[str] = None
                            ) -> None:
        """Register a Google Gemini LLM.
        
        .. deprecated:: 2.0.0
           This method is maintained for backward compatibility only and will be removed in version 3.0.0.
           Use `register_maker_by_pkd()` with protocol="google" instead:
           
           Example:
           ```python
           SimplifiedLLM.register_maker_by_pkd("my_gemini", {
               "protocol": "google",
               "api_key": api_key,
               "model_name": model_name
           })
           ```
        """
        warnings.warn(
            "register_google_llm_maker is deprecated and will be removed in version 3.0.0. "
            "Use register_maker_by_pkd with protocol='google' instead.",
            DeprecationWarning,
            stacklevel=2
        )
        cls.import_protocols()  # Ensure protocols are loaded
        cls.register_maker_by_pkd(config_name, {
            "protocol": "google",
            "api_key": api_key,
            "model_name": model_name,
            "chars_per_min_throttle": chars_per_min_throttle,
            "max_requests_per_min": max_requests_per_min,
            "shared_throttle_filepath": shared_throttle_filepath
        })

    @classmethod
    def register_fake_llm_maker(cls,
                           config_name: str,
                           response_func: Callable[[str], Union[str, Coroutine[Any, Any, str]]],
                           chars_per_min_throttle: Optional[int] = None,
                           max_requests_per_min: Optional[int] = None,
                           shared_throttle_filepath: Optional[str] = None,
                           default_temperature: Optional[float] = None
                          ) -> None:
        """Register a fake LLM for testing purposes.
        
        Args:
            config_name: Name to register this LLM configuration under
            response_func: A callable that takes a prompt string and returns a response string
            chars_per_min_throttle: Optional character limit per minute for throttling
            max_requests_per_min: Optional maximum number of requests per minute
            shared_throttle_filepath: Optional file path for cross-process throttling
        """
        def make_llm() -> FakeLLM:
            return FakeLLM(response_func=response_func)
            
        cls.register_llm_maker(
            config_name,
            make_llm,
            chars_per_min_throttle=chars_per_min_throttle,
            max_requests_per_min=max_requests_per_min,
            shared_throttle_filepath=shared_throttle_filepath,
            default_temperature=default_temperature,
        )

    @classmethod
    def register_maker_by_pkd(cls, config_name: str, packed_config: dict, allow_load_balanced: Optional[bool] = None) -> None:
        """
        Register an LLM maker based on a dictionary of configuration parameters.
        This is intended to allow you to simply forward a dictionary taken from a config file.

        Args:
            config_name: Name to register this LLM configuration under. This will override any config_name in packed_config.
            packed_config: Dictionary of configuration parameters. This dictionary will not be modified.
            allow_load_balanced: If provided, overrides any allow_load_balanced setting in packed_config.
                               If None (default), uses the value from packed_config if present, otherwise False.

        The packed config dictionary will have several pre-operations before attempting to register:
        1. Downcase all key names and snake_case-ify them (e.g. 'OpenAi' -> 'open_ai')
        2. Ignore any key name starting with an underscore (e.g. '_ignore_this_key')
        3. Convert blank string values to None
        4. Confirm presence of required 'protocol' key indicating LLM protocol type
        5. Remove protocol key from dictionary
        6. Call specific protocol maker function from the registry

        Common parameters like chars_per_min_throttle, max_requests_per_min, 
        shared_throttle_filepath, default_temperature, and allow_load_balanced can be added to any protocol.

        Example config file (config._THIS_IS_TEST01_ENV_.sh):
        ```bash
        # Azure OpenAI configuration with load balancing
        export LLM_AZURE_O4MINI_PKD="|Protocol=azure|api_base=https://your-azure1.openai.azure.com/|api_key=key1|deployment_name=gpt4|api_version=2024-02-01|allow_load_balanced=true|"
        export LLM_AZURE_O4MINI_PKD="|Protocol=azure|api_base=https://your-azure2.openai.azure.com/|api_key=key2|deployment_name=gpt4|api_version=2024-02-01|allow_load_balanced=true|"

        # OpenAI Direct configuration
        export LLM_OPENAI_O4_PKD="|Protocol=openai|api_key=your-key-2|model_name=gpt-4|"

        # Example with throttling parameters (can be added to any protocol)
        export LLM_AZURE_THROTTLED_PKD="|Protocol=azure|api_base=https://your-azure.openai.azure.com/|api_key=your-key-5|deployment_name=gpt4|api_version=2024-02-01|chars_per_min_throttle=20000|max_requests_per_min=100|shared_throttle_filepath=/tmp/llm.throttle|"
        
        # Example with default temperature
        export LLM_OPENAI_CUSTOM_PKD="|Protocol=openai|api_key=your-key|model_name=gpt-4|default_temperature=1.0|"
        ```

        Notes:
        - All PKD strings must start and end with the '|' character
        - The Protocol key is required and must match one of the supported protocols
        - Parameter names can be in either camelCase or snake_case
        - Optional throttling parameters can be added to any protocol
        - Empty values will be converted to None
        - Set allow_load_balanced=true to enable load balancing across multiple registrations with the same config_name
        - The allow_load_balanced parameter in the method signature overrides any value in the config
            
        Raises:
            ValueError: If protocol is missing or invalid, or if required parameters are missing
        """
        # Create a working copy of the config, preserving the original
        config = {k: v for k, v in packed_config.items()}
        
        def to_snake_case(name: str) -> str:
            s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
            s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1)
            return s2.lower()
        
        # Convert keys to snake_case and handle empty strings in a new dict
        processed_config = {to_snake_case(k): v for k, v in config.items() if not k.startswith('_')}
        processed_config = {k: (None if isinstance(v, str) and not v.strip() else v) for k, v in processed_config.items()}
        
        # Handle legacy input_token_limit conversion to chars_per_min_throttle
        if 'input_token_limit' in processed_config:
            token_limit = processed_config.pop('input_token_limit')
            if token_limit is not None:
                # Convert token limit to chars_per_min_throttle (roughly 4x)
                processed_config['chars_per_min_throttle'] = int(token_limit) * 4
                #logger.info(f"Converting legacy input_token_limit of {token_limit} to chars_per_min_throttle of {processed_config['chars_per_min_throttle']}")
        
        if 'protocol' not in processed_config:
            raise ValueError("Configuration dictionary must contain a 'protocol' key")
        
        protocol = processed_config.pop('protocol').lower()
        
        # Convert any numeric parameters to their proper type
        processed_config = cls._coerce_numeric_params(processed_config)
        
        # Add the config name to the parameters
        processed_config['config_name'] = config_name
        
        # Extract allow_load_balanced if present, override with parameter if provided
        config_load_balanced = processed_config.pop('allow_load_balanced', False)
        if isinstance(config_load_balanced, str):
            config_load_balanced = config_load_balanced.lower() == 'true'
        final_allow_load_balanced = allow_load_balanced if allow_load_balanced is not None else config_load_balanced
        
        # Store both the original and processed configurations
        cls._pkd_maker_params[config_name] = {
            'original': packed_config.copy(),
            'processed': processed_config.copy(),
            'protocol': protocol
        }
        
        try:
            # Get the protocol maker function and argument requirements
            maker_func, required_args, optional_args = LLMProtocolRegistry.get_protocol(protocol)
            
            # Check for required arguments
            missing_args = required_args - set(processed_config.keys())
            if missing_args:
                raise ValueError(f"Missing required arguments for protocol '{protocol}': {', '.join(missing_args)}")
            
            # Remove any arguments that aren't in required or optional sets
            valid_args = required_args | optional_args | {'config_name', 'chars_per_min_throttle', 
                                                        'max_requests_per_min', 'shared_throttle_filepath', 'default_temperature'}
            processed_config = {k: v for k, v in processed_config.items() if k in valid_args}
            
            # If allow_load_balanced is True and the config already exists,
            # we'll create a new maker and add it to the load balancer
            if final_allow_load_balanced and config_name in cls._llm_makers:
                # Create the new maker directly to avoid the duplicate name check
                throttle = None if processed_config.get('chars_per_min_throttle') is None else SelfBandwidthThrottle(
                    char_limit=processed_config['chars_per_min_throttle'],
                    interval_secs=60,
                    cross_process_logfile=processed_config.get('shared_throttle_filepath'),
                    max_requests=processed_config.get('max_requests_per_min')
                )
                
                def make_llm():
                    return maker_func(**{k: v for k, v in processed_config.items() 
                                      if k not in ['config_name', 'chars_per_min_throttle', 
                                                 'max_requests_per_min', 'shared_throttle_filepath', 'default_temperature']})
                
                default_temperature = processed_config.get('default_temperature')
                new_maker = _LLMMaker(config_name=config_name, 
                                    model_constructor=make_llm, 
                                    input_throttle=throttle,
                                    default_temperature=default_temperature)
                
                # Add to existing load balancer or create new one
                existing_maker = cls._llm_makers[config_name]
                if isinstance(existing_maker, _LoadBalancedLLMMaker):
                    existing_maker.makers.append(new_maker)
                else:
                    cls._llm_makers[config_name] = _LoadBalancedLLMMaker([existing_maker, new_maker])
            else:
                # Normal registration through the maker function
                throttle_args = {
                    'chars_per_min_throttle': processed_config.pop('chars_per_min_throttle', None),
                    'max_requests_per_min': processed_config.pop('max_requests_per_min', None),
                    'shared_throttle_filepath': processed_config.pop('shared_throttle_filepath', None),
                    'default_temperature': processed_config.pop('default_temperature', None)
                }
                
                def make_llm():
                    return maker_func(**{k: v for k, v in processed_config.items() if k != 'config_name'})
                
                cls.register_llm_maker(
                    config_name=config_name,
                    model_constructor=make_llm,
                    **{k: v for k, v in throttle_args.items() if v is not None}
                )
                
        except Exception as e:
            # Remove the stored config if registration fails
            cls._pkd_maker_params.pop(config_name, None)
            raise e

    @classmethod
    def get_maker_config(cls, config_name: str, include_processed: bool = False) -> Dict[str, Any]:
        """Retrieve the configuration used to create an LLM maker.
        
        Args:
            config_name: The name of the configuration to retrieve
            include_processed: If True, includes both the original and processed configurations
                             If False, returns only the original configuration
        
        Returns:
            If include_processed is False:
                The original configuration dictionary used to register this maker
            If include_processed is True:
                A dictionary containing:
                - 'original': The original configuration dictionary
                - 'processed': The processed configuration after snake_case conversion and other transformations
                - 'protocol': The protocol used for this configuration
                - 'is_alias': Whether this configuration is an alias
                - 'alias_of': If is_alias is True, the name of the original configuration
        
        Raises:
            ValueError: If the config_name does not exist
            KeyError: If the config_name exists but was not registered using register_maker_by_pkd
        """
        if config_name not in cls._llm_makers:
            raise ValueError(f"No configuration named '{config_name}' exists")

        # Check if this is an alias
        maker = cls._llm_makers[config_name]
        is_alias = False
        alias_of = None
        
        # If this config name isn't in _pkd_maker_params but exists in _llm_makers,
        # it might be an alias. Search through _llm_makers to find the original.
        if config_name not in cls._pkd_maker_params:
            for orig_name, orig_maker in cls._llm_makers.items():
                if orig_name != config_name and orig_maker is maker:
                    is_alias = True
                    alias_of = orig_name
                    if orig_name not in cls._pkd_maker_params:
                        raise KeyError(f"Configuration '{config_name}' is an alias of '{orig_name}', but '{orig_name}' was not registered using register_maker_by_pkd")
                    config_name = orig_name  # Use the original config name to get parameters
                    break
            if not is_alias:
                raise KeyError(f"Configuration '{config_name}' exists but was not registered using register_maker_by_pkd")

        config = cls._pkd_maker_params[config_name]
        
        if include_processed:
            return {
                'original': config['original'],
                'processed': config['processed'],
                'protocol': config['protocol'],
                'is_alias': is_alias,
                'alias_of': alias_of
            }
        else:
            return config['original']

    @classmethod
    def attach_logfile(cls,log_file:Optional[str] = None,allow_queued_writes:int = 10):
        """
        Register a logger for LLM requests.
        """
        if cls._request_logger is not None:
            cls._request_logger.flush()  # try one last time to write queued logs if any
        cls._request_logger = _LLMRequestLogger(log_file,allow_queued_writes)

    @classmethod
    def register_maker_alias(cls, 
                             config_name: str,  
                             alias_config_name: str,
                             cache_file:Optional[str] = None, 
                             cache_minutes:int = DEFAULT_CACHED_PROMPT_EXPIRATION_MINUTES,  
                             compress_strings:bool = True
                            ) -> None:
        """
        Registers an alias for a configuration name.  This allows users to use one config name
        to refer to another.  It's recommended that your alias name ends with "_CACHED" to indicate that the alias is cached.

        If a cache file is provided, the cache will be used before sending requests to the LLM.
        Cache entries expire after the specified number of minutes.
        """
        if config_name not in cls._llm_makers:
            raise ValueError(f"Config name [{config_name}] not found in registered LLM makers.  You must invoke register_maker_by_pkd() first.  Available configs are {cls.available_llm_configs()}")
        if alias_config_name in cls._llm_makers:
            raise ValueError(f"Alias config name [{alias_config_name}] already exists.  Please choose another name.")
        if cache_file is None:
            cls._llm_makers[alias_config_name] = cls._llm_makers[config_name] 
        else:
            # create a new entry, based on existing entry but with a cache
            cls._llm_makers[alias_config_name] = _LLMMaker(config_name=alias_config_name,
                                                           model_constructor=cls._llm_makers[config_name].
                                                           model_constructor, 
                                                           input_throttle = cls._llm_makers[config_name].input_throttle, 
                                                           cache= _LLMInferenceCache(cache_file,cache_minutes,compress_strings)
                                                          )

############################# Start of the "convenience" factory methods ########################################
# Below methods simply registration of common model types using vanilla parameters
# People who want more specific config should call 
#

    @classmethod
    def register_shim_llm_maker(cls, config_obj: Optional[Any],config_name:Optional[str] = None):
        """
           This method will try to search through available configs searching for suitable keys to create an LLM.  
           It will then register an LLM maker tied to the config name 'DEFAULT'.  This is a convenience method
           intended to be used by isolated test cases that don't know the details of the LLM their project prefers.
           Generally you're better off using register_llm_maker() or register_azure_llm() directly as
           this method is a bit unpredictable.

           Note that the config_obj needs to implement [] and keys() methods.
        """
        #TODO: Implementation below is a hack, could add other LLMs in the future
        config_needs = {'azure': ({"AZURE_OPENAI_API_KEY","AZURE_OPENAI_CHAT_DEPLOYMENT_NAME"},{"AZURE_OPENAI_API_VERSION"}),}
        env_keys = os.environ.keys()
        config_keys = [] if config_obj is None else config_obj.keys()
        avail_keys = set(env_keys).union(set(config_keys))
        all_parms = {k: (config_obj[k] if k in config_keys else os.environ[k]) for k in avail_keys}
        first_match = None
        for model, (req_keys, opt_keys) in config_needs.items():
            if req_keys.issubset(avail_keys):
                (relevant_keys := req_keys).update(opt_keys)
                needed_kv: dict = {k:all_parms.get(k,None) for k in relevant_keys}
                first_match = model
                break
        
        if first_match == "azure":
            def __make_azure_llm() -> BaseLLM:
                os.environ["AZURE_OPENAI_ENDPOINT"] = all_parms["AZURE_OPENAI_ENDPOINT"]
                os.environ["AZURE_OPENAI_API_KEY"] = all_parms["AZURE_OPENAI_API_KEY"]
                azure_llm=AzureChatOpenAI(
                                            openai_api_version=needed_kv["AZURE_OPENAI_API_VERSION"] ,
                                            azure_deployment=needed_kv["AZURE_OPENAI_CHAT_DEPLOYMENT_NAME"],
                                        )
                return azure_llm
            cls.register_llm_maker(config_name or cls.DEFAULT_MODEL_KEY,__make_azure_llm, shared_throttle_filepath=None)
            return
        
        #TODL: Add other LLMs here such as Direct ChatGPT

        # if we fall through to here, we couldn't find a recipe
        possible_config_sets = [f"{model} REQUIRES:{req_keys}, OPTIONAL:{opt_keys}" for model, (req_keys, opt_keys) in config_needs.items()]  
        raise ValueError(f"""Given the os.environ and the values in the config_obj provided, we don't have a recipe for building an LLM.  Possible matches are: {possible_config_sets}\n""")
                        



    @classmethod
    def create_llm_and_throttle(cls,config_name:  Optional[str] = None, default_model_fallback: bool = True) -> Tuple[BaseLLM,SelfBandwidthThrottle]:
        """Creates a connection and returns the LLM along with a throttle object that can be used for input throttling if desired."""
        original_config_name = config_name
        if config_name is None:
            config_name = cls.DEFAULT_MODEL_KEY
        elif config_name is not None and config_name not in cls._llm_makers and default_model_fallback:
            config_name = cls.DEFAULT_MODEL_KEY # fall back to default model
            logger.info(f"Config name [{original_config_name}] not found in registered LLM makers.  Falling back to default model.")
            
        if config_name not in cls._llm_makers:
            raise ValueError(f"Config name [{original_config_name}] not found in registered LLM makers.  You must invoke register_maker_by_pkd() first.  Available configs are {cls.available_llm_configs()}")
        return cls._llm_makers[config_name].model_constructor(), cls._llm_makers[config_name].input_throttle  
    

    @classmethod
    def available_llm_configs(cls) -> List[str]:
        return list(cls._llm_makers.keys())


    @classmethod    
    async def a_answer_str(
        cls,
        final_prompt_text: Union[str,Iterable[str]],
        config_name: Optional[str] = None,
        retry_max: int = 2,
        default_model_fallback: bool = True,
        temperature: Optional[float] = None,
        timeout_secs: Optional[float] = None,
        sys_prompt: Optional[str] = None,
        logging_label: Optional[str] = None  # used in event of error for logging
    ) -> str:
        """
        Asynchronous version of answer_str().
        """
        # Determine the actual config name that will be used
        actual_config_name = config_name or cls.DEFAULT_MODEL_KEY
        if actual_config_name not in cls._llm_makers and default_model_fallback:
            actual_config_name = cls.DEFAULT_MODEL_KEY
            
        # Calculate the character count for logging
        prompt_text = final_prompt_text
        if not isinstance(final_prompt_text, str) and isinstance(final_prompt_text, Iterable):
            prompt_text = "".join(final_prompt_text)
        char_count = len(prompt_text) if isinstance(prompt_text, str) else 0
        
        # Use default label if none provided
        log_label = logging_label or "~"
        
        start_time = time.time()
        try:
            result = await cls._answer(
                final_prompt_text,
                config_name,
                retry_max,
                None,
                default_model_fallback=default_model_fallback,
                temperature=temperature,
                timeout_secs=timeout_secs,
                sys_prompt=sys_prompt,
                logging_label=logging_label
            )
            # Log successful request with duration
            if cls._request_logger:
                duration = time.time() - start_time
                cls._request_logger.log_request(log_label, actual_config_name, char_count, duration)
            return result
        except Exception as e:
            # Log failed request with empty duration
            if cls._request_logger:
                cls._request_logger.log_request(log_label, actual_config_name, char_count, "")
            raise e
    
 
    
    @classmethod
    async def a_answer_struct(
        cls,
        final_prompt_text: Union[str,Iterable[str]],
        pydantic_data_class: Optional[BaseModel] = None,
        config_name: Optional[str] = None,
        retry_max: int = 2,
        parallel: int = 1,
        json_mode: bool = True,
        default_model_fallback: bool = True,
        temperature: Optional[float] = None,
        timeout_secs: Optional[float] = None,
        sys_prompt: Optional[str] = None,
        logging_label: Optional[str] = None  # used in event of error for logging
    ) -> Union[BaseModel, List[BaseModel]]:
        """
        Asynchronous version of answer_struct().
        Attempts to load the LLM response into the specified Pydantic data model.
        """
        # Determine the actual config name that will be used
        actual_config_name = config_name or cls.DEFAULT_MODEL_KEY
        if actual_config_name not in cls._llm_makers and default_model_fallback:
            actual_config_name = cls.DEFAULT_MODEL_KEY
            
        # Calculate the character count for logging
        prompt_text = final_prompt_text
        if not isinstance(final_prompt_text, str) and isinstance(final_prompt_text, Iterable):
            prompt_text = "".join(final_prompt_text)
        char_count = len(prompt_text) if isinstance(prompt_text, str) else 0
        
        # Use default label if none provided
        log_label = logging_label or "~"
        
        start_time = time.time()
        try:
            if parallel <= 1:
                retval = await cls._answer(
                    final_prompt_text,
                    config_name,
                    retry_max,
                    pydantic_data_class,
                    model_kwargs={'type': 'json_object'},
                    json_mode=json_mode,
                    default_model_fallback=default_model_fallback,
                    temperature=temperature,
                    timeout_secs=timeout_secs,
                    sys_prompt=sys_prompt,
                    logging_label=logging_label
                )
            else:
                # If parallel > 1, set retry_max to 1 regardless of the passed value
                tasks = [
                    cls._answer(
                        final_prompt_text,
                        config_name,
                        retry_max=1,
                        parser=pydantic_data_class,
                        model_kwargs={'type': 'json_object'},
                        json_mode=json_mode,
                        default_model_fallback=default_model_fallback,
                        temperature=temperature,
                        sys_prompt=sys_prompt,
                        timeout_secs=timeout_secs,
                        logging_label=logging_label
                    ) for _ in range(parallel)
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                retval = [v for v in results if not isinstance(v, Exception)]
                if len(retval) == 0:
                    raise results[0]  # Re-raise the first exception
            
            # Log successful request with duration
            if cls._request_logger:
                duration = time.time() - start_time
                cls._request_logger.log_request(log_label, actual_config_name, char_count, duration)
            
            return retval  # Could be a single item or a list
        except Exception as e:
            # Log failed request with empty duration
            if cls._request_logger:
                cls._request_logger.log_request(log_label, actual_config_name, char_count, "")
            raise e

    @classmethod
    async def _answer(
        cls,
        final_prompt_text: Union[str,Iterable[str]],
        config_name: Optional[str],
        retry_max: int,
        parser: Optional[BaseModel],
        model_kwargs: Optional[Dict[str, Any]] = None,
        parallel: int = 1,
        json_mode: bool = False,
        default_model_fallback: bool = True,
        temperature: Optional[float] = None,
        timeout_secs: Optional[float] = None,
        sys_prompt: Optional[str] = None,  # ignored if empty string or None
        logging_label: Optional[str] = None  # used in event of error for logging
    ):
        """Does the lifting for other answer...() methods.
        Note that if final_prompt_text is an iterable, it will be presumed to be a sys_prompt followed by a user prompt.
        """
        if model_kwargs is None:
            model_kwargs = {}

        if parallel > 1:
            retry_max = 1  # Override retry_max for parallel execution
        #DEVELOPER NOTE: parallel handling is higher in the call stack?  why is the above here?

        if not isinstance(final_prompt_text,str) and isinstance(final_prompt_text, Iterable):
            final_prompt_text = list(final_prompt_text)
            if len(final_prompt_text) == 2:
                sys_prompt, final_prompt_text = final_prompt_text
            elif len(final_prompt_text) == 1:
                final_prompt_text = final_prompt_text[0]
                sys_prompt = None
            else:
                raise ValueError("If final_prompt_text is an iterable, it should have exactly 2 items with the first interpreted as sys_prompt.")

        # Determine the actual config name that will be used (after fallback)
        actual_config_name = config_name or cls.DEFAULT_MODEL_KEY
        if actual_config_name not in cls._llm_makers and default_model_fallback:
            actual_config_name = cls.DEFAULT_MODEL_KEY
        
        llm, throttle = cls.create_llm_and_throttle(config_name, default_model_fallback)

        invoke_args: Dict[str, Any] = {}
        invoke_args.update(model_kwargs)

        is_chat_model = isinstance(llm, BaseChatModel)
        cache_prompt_text = final_prompt_text
        plain_prompt = final_prompt_text

        # Use default_temperature from maker if temperature is not explicitly provided
        if temperature is None and actual_config_name in cls._llm_makers:
            maker = cls._llm_makers[actual_config_name]
            # Handle load-balanced makers
            if isinstance(maker, _LoadBalancedLLMMaker):
                # Use the first maker's default temperature
                maker = maker.makers[0] if maker.makers else maker
            if maker.default_temperature is not None:
                temperature = maker.default_temperature

        # Bind temperature and response_format to the LLM instance
        # Temperature must be bound to the LLM, not passed as invoke_args
        bind_kwargs = {}
        if temperature is not None:
            bind_kwargs['temperature'] = temperature
        if json_mode and is_chat_model:
            bind_kwargs['response_format'] = {"type": "json_object"}
        
        # Create a bound LLM instance with the specified parameters
        if bind_kwargs:
            llm = llm.bind(**bind_kwargs)

        if is_chat_model:
            # Construct the message list
            messages = []
            if sys_prompt:  # ignores empty string or None
                system_message = SystemMessage(content=sys_prompt)
                messages.append(system_message)

            user_message = HumanMessage(content=final_prompt_text)
            messages.append(user_message)
            # Create a ChatPromptTemplate from messages
            chat_prompt:ChatPromptTemplate = ChatPromptTemplate.from_messages(messages) 

            # Initialize the chain with the chat prompt and LLM
            chain = chat_prompt | llm # | RepairingJsonOutputParser()
        else:
            if sys_prompt:
                plain_prompt = f"{sys_prompt}\n\n{final_prompt_text}"
            cache_prompt_text = plain_prompt
            if json_mode:
                invoke_args.setdefault('response_format', {"type": "json_object"})

        for attempt_num in range(retry_max):
            if throttle:
                await throttle.sleep_on_throttle(len(cache_prompt_text))  # Potentially async sleep here
            try:
                # Check for cache first
                cache, cached_result = None, None
                if (cache:= cls._llm_makers[config_name].cache) and (cached_result:= cache.get_entry(cache_prompt_text)):
                    raw_result = cached_result
                else:
                    if is_chat_model:
                        maybe_coro = chain.ainvoke({}, **invoke_args)
                        if asyncio.iscoroutine(maybe_coro) or isinstance(maybe_coro, Awaitable):
                            raw_result = await asyncio.wait_for(
                                maybe_coro,
                                timeout=(timeout_secs or DEFAULT_LLM_TIMEOUT),
                            )
                        else:
                            raw_result = maybe_coro
                    else:
                        maybe_coro = llm.ainvoke(plain_prompt, **invoke_args)
                        if asyncio.iscoroutine(maybe_coro) or isinstance(maybe_coro, Awaitable):
                            raw_result = await asyncio.wait_for(
                                maybe_coro,
                                timeout=(timeout_secs or DEFAULT_LLM_TIMEOUT),
                            )
                        else:
                            raw_result = maybe_coro

                if isinstance(raw_result,AIMessage):
                    raw_result = raw_result.content

                if json_mode:
                    # convert raw results into a dictionary
                    parsed_result:dict = RepairingJsonOutputParser().parse(raw_result)
                    if not isinstance(parsed_result,dict):
                        raise ValueError(f"Unable to translate the LLM's response into a dictionary (logging_label={logging_label}) from raw result:\n{raw_result}")
                      
                    if parser is not None: # if pydantic class provided, use it to validate/parse
                        try: 
                            parsed_result = parser(**parsed_result) # what is called a "parser" here is actually a pydantic model
                        except Exception as e:
                            if hasattr(e, 'add_note'):
                                e.add_note(f"Error parsing structured pydantic data class [{parser.__name__}] out of dict formed from:\n {raw_result}")
                            raise e  # Forward the exception
                        return parsed_result
                else:
                    parsed_result = raw_result if isinstance(raw_result,str) else raw_result.content  # Extract content if not in JSON mode

                if cache and not cached_result:
                    # store the result in the cache (if one is available)
                    cache.store_entry(cache_prompt_text,parsed_result)
                return parsed_result

            except Exception as e:
                if isinstance(e, asyncio.TimeoutError):
                    logger.info(f"LLM call timeout of {timeout_secs or DEFAULT_LLM_TIMEOUT} seconds exceeded (logging_label={logging_label}).")
                if attempt_num >= retry_max - 1:
                    logger.error(f"Aborting due to error during LLM call (logging_label={logging_label}) due to exception {type(e).__name__}: {str(e)}")
                    raise e
                else:
                    if not isinstance(e, asyncio.TimeoutError): # don't bother printing this message for timeouts
                        logger.info(f"Retrying LLM call (logging_label={logging_label}) due to exception {type(e).__name__}: {str(e)}")
                    await asyncio.sleep(0.15)  # Arbitrary tiny pause before retry

    @classmethod
    def find_json_in_str(cls, text: str) -> Dict:
        """
        Searches for exactly one JSON dictionary within a multiline string based on specific rules.
        
        Rules:
        1. Opening curly brace is the first non-whitespace character on a line.
        2. The next non-whitespace character after the curly brace is a double-quote character.
           This character may be on the same line or a subsequent line.
        
        Args:
            text (str): The multiline string to search within.
        
        Returns:
            dict: The parsed JSON dictionary.
        
        Raises:
            ValueError: If no JSON dictionary is found or multiple are found.
            json.JSONDecodeError: If the JSON found is invalid.
        """
        lines = text.splitlines()
        json_start_indices = []
        json_objects = []
        
        # Pattern to detect a line starting with '{' after optional whitespace
        start_pattern = re.compile(r'^\s*\{')

        for i, line in enumerate(lines):
            if start_pattern.match(line):
                # Potential start of JSON object
                start_index = i
                # Check for the next non-whitespace character after '{'
                # This may span multiple lines
                # Combine lines starting from start_index
                potential_json = '\n'.join(lines[start_index:])
                try:
                    # Use a JSON decoder to find where the JSON object ends
                    decoder = json.JSONDecoder()
                    obj, end = decoder.raw_decode(potential_json)
                    # Verify that after '{', the next non-whitespace character is '"'
                    # Find the position of '{'
                    brace_pos = potential_json.find('{')
                    # Find the position of the first '"' after '{'
                    quote_pos = potential_json.find('"', brace_pos + 1)
                    if quote_pos == -1:
                        continue  # No quote found after '{', invalid start
                    # Ensure that between '{' and '"' there are only whitespace characters
                    if not potential_json[brace_pos + 1:quote_pos].strip() == '':
                        continue  # Non-whitespace characters between '{' and '"', invalid start
                    # If all checks pass, add the object
                    json_objects.append(obj)
                except json.JSONDecodeError:
                    continue  # Not a valid JSON starting here
        
        if len(json_objects) == 0:
            raise ValueError("No valid JSON object found in the text:\n" + text)
        elif len(json_objects) > 1:
            raise ValueError("Multiple JSON objects found in the text.")
        else:
            return json_objects[0]

    @classmethod
    async def sleep_on_throttle(cls, request_chars: int) -> None:
        """
        Sleeps if the throttle is encountered to expire before returning.
        Use suggest_throttle_seconds() if sleeping is not what you want to do.

        BE AWARE: This method is deliberately async to allow for other async operations to continue while waiting.
        
        Args:
            request_chars: Number of characters in the planned request
        """
        throttle_seconds = cls.suggest_throttle_seconds(request_chars)
        if throttle_seconds is not None and throttle_seconds > 0:
            logger.info(f"Self-throttling sleep() triggered for {throttle_seconds:.1f} seconds in order to fit {request_chars:_} into interval limit of {cls.char_limit:_} chars.")
            await asyncio.sleep(throttle_seconds)