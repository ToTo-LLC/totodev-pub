# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

# simple_llm_prompt.py
from typing import Any, Callable, Union, List, Optional, TextIO, Set, Self, Tuple,Generator,Dict
from abc import ABC, abstractmethod
from pydantic import BaseModel, Field
from jinja2 import Template as J2Template
import click
import sys
import glob
import yaml
import re
import itertools
import os
import time
from time import strftime, gmtime
from datetime import datetime
import requests
from totodev_pub.logger import MyLogger
from io import BytesIO
# from Langfuse import Langfuse   # don't need this, go directly against restful API


logger = MyLogger.shared_logger()

# For YAML/dict format, these are the keys that will be searched for the prompt string(s)
USER_PROMPT_KEYS = ('user_prompt', 'template','prompt','prompty_prompt')
SYS_PROMPT_KEYS = ('sys_prompt', 'sys_template','system_prompt','system_template')

# Below setting is used to prevent langfuse from being accessed in production and other environments
# for which dynamic retrieval of prompts would be an unacceptable security risk.
ENV_TYPE_TO_POLL_LANGFUSE = ['DEV']  # check os.environ['ENV_TYPE'], if no match, no retrieve.  If empty, raise error

class SimpleLLMPrompt(ABC):
    """
    Do not create instances of this class directly.  Instead use:
    - from_file()
    - from_dict()
    - from_prompt() 

    This class provides a simple mechanism to allow serializing prompts to a YAML or other format.
    'Simple' is meant in reference to the developer experience, not the services provided.
    This code makes informed guesses about the structure of prompt files.  

    The class relies upon the existence of a "prompt file" that contains a user prompt and optionally a system prompt.
    It may also contain other metadata, but this class mostly ignores that.
    Finally, it may include in its metadata keys for Langfuse that cause it to attempt to retrieve and update the local file's prompt

    File Assumptions:
      - Prompt files are either:
        - Flat text file containing only the user prompt.
          - If the first character in the file is a hash, then succeeding lines starting with a hash are ignored until a line is encountered without a hash.
        - YAML file ending with the file suffix ".yaml"
        - File names may be versioned within filename by an alpha-sortable date or version number (e.g. 'v2024-01-17' or 'v001')    
        - Files may contain a 'system' prompt but MUST contain a 'user' prompt

    Prompt String Assumptions:
        - Uses jinja2 style merge variable syntax (e.g. {{var_name}})
        - The prompt string may contain multiple merge variables
        - The merge variables are simple variable names only, not expressions

    YAML Assumptions:
        - File will be valid parseable YAML (with non-standards as noted below)
        - Most non-prompt data in the YAML is ignored by the program (see Langfuse exception below)
        - Data from the YAML file may be retrieved via Dict-like access (e.g. prompt['key_name'])
        - Prompts may be stored under key names at the root of the YAML or at the end as Prompty style.
        - If stored in key names:
            - See USER_PROMPT_KEYS for search order of user prompts
            - See SYS_PROMPT_KEYS for search order of system prompts
            - If no USER_PROMPT_KEYS are found, the prompt is assumed to be a "Prompty" style prompt
        - If stored in Prompty style:
            - Prompt(s) are stored as a raw string following the '---' YAML marker at the end of the YAML data
            - If only one prompt is present it is assumed to be the user prompt
            - If two prompts are present, the first is assumed to be the system prompt and the second the user prompt
            - The second prompt, if present, follows the first prompt after encountering a line with only a '---' marker.
            - User prompt will be stored and accessible under any of the USER_PROMPT_KEYS
            - System prompt will be stored and accessible under any of the SYS_PROMPT_KEYS 

    Langfuse Integration:
      The program can work with a Langfuse repository to retrieve and update prompt files on first access of object.
      This will MODIFY the prompt file using Prompty style format.
      - Triggered by the presence of a 'langfuse' key in the prompt file, indicating a prompt name to retrieve via langfuse's get_prompt().
      - Several other keys are recognized but optional( LANGFUSE_PUBLIC_KEY,LANGFUSE_SECRET_KEY,LANGFUSE_HOST)

    Langfuse Mechanism:
        - If the file contains a 'langfuse' key, the program will attempt to retrieve the prompt from langfuse.
        - If Langfuse is inaccessible, the program quietly falls back to the local file.
        - Langfuse access (or failure) appear in the logger stream as INFO messages.
        - Some optimizations were made to reduce overly frequent calls to Langfuse.
        - If the optional langfuse keys are not provided in the YAML file, the info must be provided in environment variables.

    """

    # Below are optional and can be added via register_langfuse_creds()
    langfuse_public_key: Optional[str] = None
    langfuse_secret_key: Optional[str] = None
    langfuse_host: Optional[str] = None

    @classmethod
    def register_langfuse_creds(cls,public_key:str,secret_key:str,host:str) -> None:
        """
        Register the Langfuse credentials.
        """
        cls.langfuse_public_key = public_key
        cls.langfuse_secret_key = secret_key
        cls.langfuse_host = host

    @abstractmethod
    def render(self, **kwargs) -> Union[str, List[str]]:
        """
        Render the prompt string, merging in the provided variables.  

        Note that generally no check is performed to see if the provided variable names match the expected variables in the prompt.
        
        However, if the verify_var_names_period parameter was set to a positive integer then name matching is verified periodically.
        This is done by checking the variable names in the prompt string against the provided variable names on every Nth call to render().

        The method returns a string if there is no system prompt, or a list of two strings if there is a system prompt.
        In that case, the first string is the system prompt, the second string is the user prompt.
        """
        pass

    @abstractmethod
    def __getitem__(self, key) -> Any:
        pass

    def get(self,key,default=None) -> Any:
        """Return the value for the key if it exists, otherwise return the default value."""
        return self[key] if key in self else default

    @abstractmethod
    def keys(self) -> Generator[Any,None,None]:
        """"At minimum, contains an entry for the user prompt but may also contain an entry for the system prompt and YAML key-values."""
        pass

    @abstractmethod
    def __contains__(self, key) -> bool:
        """Checks for the key witin keys()."""
        pass

    @abstractmethod
    def infer_var_names(self) -> Set[str]:
        pass

    def user_prompt(self) -> str:
        """Return the user prompt string (pre-render)."""
        # look for the first key from the USER_PROMPT_KEYS
        for key in USER_PROMPT_KEYS:
            if key in self:
                return self[key]
        raise KeyError("No user prompt found in prompt file.")
    
    def sys_prompt(self) -> Optional[str]:
        """Return the system prompt string (pre-render)."""
        # look for the first key from the SYS_PROMPT_KEYS
        for key in SYS_PROMPT_KEYS:
            if key in self:
                return self[key]
        return None  # system prompt is optional

    def as_dict(self) -> Dict[str,Any]:
        """Return the metadata dictionary."""
        # derived classes may override for efficiency if desired
        return {key: self[key] for key in self.keys()}


    @classmethod
    def from_file(cls, file_glob: str, verify_var_names_period: Optional[int] = None, lazy: bool = True,relative_to:Optional[str] = None) -> Self:
        """
        Reads a prompt from a file. The file type is determined by the extension.
        If the path is a glob pattern, then the last file in the sorted list of matches is chosen.
        This is done so that simple prompt file versioning schemes may be used such as: prompt_2024-07-21.yaml or prompt_v001.yaml
        If filename ends in .yaml or .yml, it will be loaded as YAML.
        
        If lazy is True, then this method will return a LazyLoadSimpleLLMPrompt instance that will only load the prompt when needed.
        If relative_to is provided, the file_glob will be treated as relative to that path or file.
        """
        # exact file is chosen at this point (rather than at first use)
        if relative_to:
            if os.path.isdir(relative_to):
                file_glob = os.path.join(relative_to,file_glob)
            else:
                file_glob = os.path.join(os.path.dirname(relative_to),file_glob)

        source_file = _SimpleLLMPromptImpl._choose_file(file_glob) # chooses "newest" file if glob pattern
        def _gen_instance():
            return _SimpleLLMPromptImpl._from_file(source_file, verify_var_names_period=verify_var_names_period)
        return _LazyLoadSimpleLLMPromptProxy(_gen_instance) if lazy else _gen_instance()

    @classmethod
    def from_dict(cls, prompt_dict: dict, verify_var_names_period: Optional[int] = None) -> Self:
        """
        Create a SimpleLLMPrompt instance from a dictionary containing the prompt data.
        Must contain the required keys
        """
        return _SimpleLLMPromptImpl(prompt_dict, verify_var_names_period=verify_var_names_period)


    @classmethod
    def from_prompt(cls, user_prompt: str, sys_prompt:str = None, verify_var_names_period: Optional[int] = None) -> Self:
        """
        Create a SimpleLLMPrompt instance from a raw prompt string (and optionally a system prompt string).
        """
        prompt = {'prompt': user_prompt}
        if sys_prompt:
            prompt['sys_prompt'] = sys_prompt
        return _SimpleLLMPromptImpl(prompt, verify_var_names_period=verify_var_names_period)
    

    @classmethod
    def from_langfuse(cls, prompt_name: str, public_key: Optional[str] = None, secret_key: Optional[str] = None, host: Optional[str] = None, _stub_langfuse_prompt_response=Optional[dict]) -> Self:
        """
        Create a SimpleLLMPrompt instance from a prompt retrieved from Langfuse.

        For library maintainers, use _stub_langfuse_prompt_response for testing
        """
        def _prompts_data_to_impl(prompts_data: Any) -> "_SimpleLLMPromptImpl":
            if isinstance(prompts_data, dict) and "prompt" in prompts_data:
                return _SimpleLLMPromptImpl(prompts_data)
            if isinstance(prompts_data, tuple) and len(prompts_data) == 2:
                sys_prompt, user_prompt = prompts_data
                prompts_dict = {"prompt": user_prompt}
                if sys_prompt:
                    prompts_dict["sys_prompt"] = sys_prompt
                return _SimpleLLMPromptImpl(prompts_dict)
            raise TypeError(f"Unexpected prompts format: type={type(prompts_data)}, value={prompts_data}")

        lf_retriever = LangfusePromptRetriever(prompt_name, 
                                               public_key or cls.langfuse_public_key, 
                                               secret_key or cls.langfuse_secret_key, 
                                               host or cls.langfuse_host
                                              )
        if _stub_langfuse_prompt_response:
            assert isinstance(_stub_langfuse_prompt_response, dict)
            try:
                lf_retriever._parse_and_cache_prompt_response(_stub_langfuse_prompt_response)
                return _prompts_data_to_impl(lf_retriever._cached_prompt)
            except Exception as e:
                logger.error(f"Error processing prompts: {e}")

        if not lf_retriever:
            # It's better to catch this particular error near the point of declaration rather than lazily
            raise ValueError("Missing required API credentials to retrieve prompt from Langfuse.  These must be explicitly passed in or retrieved from environment strings LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_HOST.")

        def _gen_instance():  # deferred construction
            try:
                return _prompts_data_to_impl(lf_retriever.prompts)
            except Exception as e:
                logger.error(f"Error fetching/processing prompts: {e}")

        return _LazyLoadSimpleLLMPromptProxy(_gen_instance)
    

    # Dictionary mapping model types to lambda functions for prompt generation
    PROMPT_GENERATORS: Dict[str, Callable[[str, str], str]] = {
        "llama": lambda sp, up: f"[INST] <<SYS>>\n{sp}\n<</SYS>>\n\n{up} [/INST]",
        "claude": lambda sp, up: f"Human: {sp}\n\n{up}\n\nAssistant:",
        "gpt": lambda sp, up: f"System: {sp}\n\nHuman: {up}\n\nAssistant:",
        "default": lambda sp, up: f"{sp}\n\n{up}"
    }

    @classmethod
    def make_crude_prompt(cls, model_type: str, user_prompt: str, sys_prompt: Optional[str] = None) -> str:
        """
        Generate a crude prompt for various LLM models through simple string concatenation.
        Note that it is often better to use native model APIs for prompt generation.

        Args:
        model_type (str): The type of the model (e.g., "llama", "claude", "gpt").
        user_prompt (str): The user's prompt.
        sys_prompt (Optional[str]): The system prompt (optional).

        Returns:
        str: The generated prompt.

        Raises:
        ValueError: If the model_type is not supported.
        """
        if model_type not in cls.PROMPT_GENERATORS:
            supported_models = ", ".join(cls.PROMPT_GENERATORS.keys())
            raise ValueError(f"Unsupported model type: {model_type}. Supported types are: {supported_models}")

        generator = cls.PROMPT_GENERATORS[model_type]
        # Handle the case where sys_prompt is None
        effective_sys_prompt = sys_prompt if sys_prompt is not None else ""
        return generator(effective_sys_prompt, user_prompt)

    def make_crude_prompt(self) -> str:
        """see classmethod make_crude_prompt"""
        return self.make_crude_prompt(self.model_type, self.user_prompt, self.sys_prompt)

#
# End of SimpleLLMPrompt class
#
#######################################################


class _LazyLoadSimpleLLMPromptProxy(SimpleLLMPrompt):
    """
    Proxy class that defers creation of the SimpleLLMPrompt instance until it is needed.
    Users are actually given one of these objects when they call SimpleLLMPrompt.from_file(lazy=True).
    Ultimately, all functionality is provided by the SimpleLLMPrompt instance that is created when needed.
    """
    def __init__(self, instance_creator: Callable[[], SimpleLLMPrompt]):
        self._instance_creator = instance_creator
        self._instance: Optional[SimpleLLMPrompt] = None

    #TODO: reimplent the below stuff using `from functools import wraps` and `@wraps` decorator

    def _get_instance(self) -> SimpleLLMPrompt:
        if self._instance is None:
            self._instance = self._instance_creator()
            self._instance_creator = None  # release the creator
        return self._instance

    def render(self, **kwargs) -> Union[str, List[str]]:
        inst = self._get_instance()
        return inst.render(**kwargs)

    def __getitem__(self, key) -> Any:
        return self._get_instance()[key]
    
    def keys(self) -> Generator[Any,None,None]:
        return self._get_instance().keys()

    def __contains__(self, key) -> bool:
        return self._get_instance().__contains__(key)

    def infer_var_names(self) -> Set[str]:
        return self._get_instance().infer_var_names()

    def __repr__(self) -> str:
        inst = self._get_instance()
        return inst.__repr__().replace(inst.__class__.__name__, self.__class__.__name__)
#
# End of SimpleLLMPromptProxy class
#
#######################################################    


class _PromptInfo(BaseModel):
    raw_sys_prompt: Optional[str] = None
    raw_prompt: Optional[str] = None


    def as_list(self) -> List[str]:
        return [self.raw_sys_prompt, self.raw_prompt]

    def infer_var_names(self) -> Set[str]:
        prompts = self.as_list()
        var_names = set()
        for prompt in prompts:
            if prompt:
                var_names.update(re.findall(r'{{\s*(\w+)\s*}}', prompt))
        return var_names
#
# End of _PromptInfo class
#
#######################################################
    

class _SimpleLLMPromptImpl(SimpleLLMPrompt):
    DEFAULT_VERIFY_NAMES_PERIOD = 10  # every 10th call to render() will verify variable names

    def __init__(self, prompt_or_meta: Union[str, dict], verify_var_names_period: Optional[int] = None, source_file: Optional[str] = None):
        """
        Create from either a simple raw prompt template string or a dictionary with prompt and metadata keys.
        If dictionary, then a valid prompt key must be present in the root keys of the dict.

        See the render() method for an explanation of how the verify_var_names_period parameter is used.
        """
        self._meta: dict = prompt_or_meta if isinstance(prompt_or_meta, dict) else {'prompt': prompt_or_meta}
        self._verify_var_names_period: int = verify_var_names_period or self.DEFAULT_VERIFY_NAMES_PERIOD
        self._next_verify_countdown: int = 0  # indicates next verification to be done
        self.source_file: Optional[str] = source_file

    def raw(self) -> _PromptInfo:
        prompt_info = _PromptInfo()
        # find the prompt string within the meta dict (must be present)
        for key in USER_PROMPT_KEYS:
            if key in self._meta:
                prompt_info.raw_prompt = self._meta[key]
                break
        # find the system prompt string within the meta dict (optional)
        for key in SYS_PROMPT_KEYS:
            if key in self._meta:
                prompt_info.raw_sys_prompt = self._meta[key]
                break
        return prompt_info


    def render(self, **kwargs) -> Union[str, List[str]]:
        prompt_info: _PromptInfo = self.raw()
        if self._verify_var_names_period > 0:
            # periodically verify provided variable names match expected variable names
            if self._next_verify_countdown == 0:
                self._next_verify_countdown = self._verify_var_names_period
                var_names = prompt_info.infer_var_names()
                provided_var_names = set(kwargs.keys())
                missing_vars = var_names - provided_var_names
                unrecognized_vars = provided_var_names - var_names
                miss_msg = f"Missing variables from your params: {missing_vars}." if missing_vars else ''
                unrec_msg = f"Extra variable names found in your parameter list: {unrecognized_vars}." if unrecognized_vars else ''
                if missing_vars or unrecognized_vars:
                    raise ValueError(f"Variable name mismatch in {self.__repr__()}. {miss_msg} {unrec_msg}")
            self._next_verify_countdown -= 1

        # merge in the provided variables assuming jinja2 style double curly brackets
        rendered_prompts = [J2Template(prompt).render(**kwargs) for prompt in prompt_info.as_list() if prompt]
        return rendered_prompts[0] if len(rendered_prompts) == 1 else rendered_prompts


    def infer_var_names(self) -> Set[str]:
        prompt_info = self.raw()
        return prompt_info.infer_var_names()

    def __getitem__(self, key) -> Any:
        return self._meta[key]

    def keys(self) -> Generator[Any,None,None]:
        return self._meta.keys()

    def __contains__(self, key) -> bool:
        return key in self._meta

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        if self.source_file is not None:
            return f"{class_name}(source_file=`{self.source_file}`)"
        else:
            prompt_str = self.raw().raw_prompt or ''
            return f"{class_name}('{prompt_str[:50]}{'...' if len(prompt_str) > 50 else ''}')"

    @classmethod
    def _choose_file(cls, file_path: str) -> str:
        # Assume filepath is a glob pattern. Find all matches and choose last by alpha sort
        files = sorted(glob.glob(file_path))
        if not files:
            raise FileNotFoundError(f"No files found matching pattern: {file_path}")
        return files[-1]

    @classmethod
    def _from_file(cls, file_path: str, verify_var_names_period: Optional[int] = None,allow_langfuse_fetch:bool = True) -> Self:
        """
        verify_var_names_period:  determines how often the render() method will verify that the provided variable names match the expected variable names.
        """
        new_obj:cls = None
        with open(file_path, 'r') as f:
            if file_path.endswith('.yaml') or file_path.endswith('.yml'):
                try:
                    yaml_dict = cls._dict_from_yaml_file(f)
                except Exception as e:
                    # Note luigi's communication of error is not very helpful.  This is a workaround.
                    msg = f"Failed to parse the YAML portion of the prompt file ({file_path})."
                    logger.error(msg)
                    raise ValueError(msg) from e
                new_obj = cls(yaml_dict, verify_var_names_period=verify_var_names_period, source_file=file_path)
            else:
                # read lines until first non-hash line encountered
                for line in f:
                    if not line.startswith('#'):
                        break
                    line = ''
                body = line + f.read()
                new_obj = cls({USER_PROMPT_KEYS[0]: body},
                           verify_var_names_period=verify_var_names_period,
                           source_file=file_path)
        if allow_langfuse_fetch and 'langfuse' in new_obj and new_obj['langfuse']:
            _langfuse_receiver = LangfusePromptRetriever(new_obj['langfuse'])
            pf_writer = _PromptFileWriter(file_path, langfuse_retriever=_langfuse_receiver,prompt_obj=new_obj)
            try:
                # if the current local file is older and the Langfuse has newer prompts, update the file
                if pf_writer.needs_langfuse_update() and _langfuse_receiver.has_changed(new_obj.sys_prompt(),new_obj.user_prompt()):
                    buffer = BytesIO()
                    pf_writer.write_prompt_file(buffer)
                    buffer.seek(0)
                    with open(file_path, 'wb') as f:
                        f.write(buffer.read())
                    new_obj = cls._from_file(file_path, verify_var_names_period=verify_var_names_period, allow_langfuse_fetch=False)
            except Exception as e:
                # log the error but don't raise it
                logger.warning(f"Failed to update prompt file ({file_path}) with Langfuse prompt name [{new_obj['langfuse']}].  Error:\n {e}")  
                pass # deliberate no-op... just quietly fail

        return new_obj
    
    @classmethod
    def _dict_from_yaml_file(cls,f:TextIO) -> dict:
        docs = yaml.safe_load_all(f) # loads potentially many
        try:
            yaml_dict = next(docs) # get the first one
        except StopIteration as e:
            e.add_note("No YAML data found during attempt to read the file.")
            raise 
        if not any(key in yaml_dict for key in USER_PROMPT_KEYS):
            # check for 'Prompty' style prompt (where prompt template follows midfile '---' YAML marker)
            f.seek(2)  # go back to start but skip the first '---' if one is present.
            sep_count:int = 0 # seperator is '---' at start of line
            prompt_lines = [[],[]] # user and system prompt lines
            for line in f:
                if line.rstrip() == '---':
                    sep_count += 1  # never append these lines
                    if sep_count > 2:
                        break  # ignore any further '---' markers
                else: # just reaing regular line
                    if sep_count == 1 or sep_count == 2:
                        prompt_lines[sep_count-1].append(line)
            if prompt_lines:
                yaml_dict[USER_PROMPT_KEYS[0]] = ''.join(prompt_lines.pop()).strip() or None
                if prompt_lines:
                    yaml_dict[SYS_PROMPT_KEYS[0]] = ''.join(prompt_lines.pop()).strip() or None
        return yaml_dict

#
# End of _SimpleLLMPromptImpl class
#
#######################################################



class LangfusePrompt(BaseModel):
    """Represents a prompt retrieved from Langfuse"""
    system_content: Optional[str] = None
    user_content: str
    version: int
    labels: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    updated_at: datetime
    project_name: Optional[str] = None

class LangfusePromptRetriever:
    def __init__(self, prompt_name: str,
                 public_key: Optional[str] = None,
                 secret_key: Optional[str] = None,
                 host: Optional[str] = None,
                 retrieve_newest_with_label: str = 'production',
                 env_type_allow_prompt_retrieval: List[str] = ENV_TYPE_TO_POLL_LANGFUSE,
                 mock_langfuse_prompt: Optional[LangfusePrompt] = None
                 ) -> None:
        """
        Initialize with either real Langfuse credentials or a mock prompt.
        
        Args:
            mock_langfuse_prompt: If provided, this prompt will be used instead of 
                                retrieving from Langfuse. Useful for testing and development.
        """
        self._retrieve_label = retrieve_newest_with_label
        self.prompt_name = prompt_name
        self._mock_prompt = mock_langfuse_prompt
        
        if mock_langfuse_prompt is None:
            # Only set up Langfuse connection if we're not using a mock
            self._public_key = public_key or SimpleLLMPrompt.langfuse_public_key or os.getenv("LANGFUSE_PUBLIC_KEY")
            self._secret_key = secret_key or SimpleLLMPrompt.langfuse_secret_key or os.getenv("LANGFUSE_SECRET_KEY")
            self._host = host or SimpleLLMPrompt.langfuse_host or os.getenv("LANGFUSE_HOST")
            
            if os.getenv('ENV_TYPE',None) not in env_type_allow_prompt_retrieval and '*' not in env_type_allow_prompt_retrieval:
                raise ValueError(f"The os.environ['ENV_TYPE'] must be one of: {env_type_allow_prompt_retrieval}")
        
        # Initialize cached values
        self._cached_prompt: Optional[Tuple[str, str]] = None
        self.project_name: Optional[str] = mock_langfuse_prompt.project_name if mock_langfuse_prompt else None
        self.version: Optional[int] = mock_langfuse_prompt.version if mock_langfuse_prompt else None
        self.labels: Optional[List[str]] = mock_langfuse_prompt.labels if mock_langfuse_prompt else None
        self.tags: Optional[List[str]] = mock_langfuse_prompt.tags if mock_langfuse_prompt else None
        self.updated_at: Optional[str] = mock_langfuse_prompt.updated_at.isoformat() + 'Z' if mock_langfuse_prompt else None

    def fetch_prompts(self) -> Tuple[str,str]:
        """Contact Langfuse to retrieve the prompt string, or return mock if provided."""
        if self._mock_prompt:
            self._cached_prompt = (
                self._mock_prompt.system_content,
                self._mock_prompt.user_content
            )
            return self._cached_prompt

        logger.info(f"Fetching prompt from Langfuse [{self._host}] for prompt name: {self.prompt_name}")
        if not self.project_name:
            url = f"{self._host}/api/public/projects"
            try:
                response = requests.get(url, auth=(self._public_key, self._secret_key))
                response.raise_for_status()
                data = response.json()
                if "data" not in data:
                    raise RuntimeError("The API response is missing the 'data' field.")
                self.project_name = data["data"][0]["name"]
            except requests.RequestException as e:
                e.args = (
                    f"Failed to fetch project name from Langfuse.\n"
                    f"URL: {url}\n"
                    f"Error: {e}\n"
                    f"Response Text: {getattr(e.response, 'text', 'N/A')}",
                ) + e.args
                raise

        url = f"{self._host}/api/public/v2/prompts/{self.prompt_name}"
        try:
            response = requests.get(url,
                                    params = {'label': self._retrieve_label}, 
                                    auth=(self._public_key, self._secret_key)
                                   )
            response.raise_for_status()
        except requests.RequestException as e:
            e.args = (
                f"Failed fetching prompt from Langfuse  Note credentials were confirmed validate.  Common problems are a bad prompt name or the named prompt has not been marked 'production'.\n"
                f"URL: {url}\n"
                f"Prompt Name: {self.prompt_name}\n"
                f"Error: {e}\n"
                f"Response Text: {getattr(e.response, 'text', 'N/A')}",
            ) + e.args
            raise
        self._parse_and_cache_prompt_response(response.json())
        return self._cached_prompt

    def _parse_and_cache_prompt_response(self, api_response_data: dict, proj_name: Optional[str] = None) -> None:
        """Convert API response into a LangfusePrompt structure"""
        if self._mock_prompt:
            return  # No need to parse if using mock
            
        if proj_name:
            self.project_name = proj_name
        self._cached_prompt = None  # preclear
        if "prompt" not in api_response_data:
            raise RuntimeError("Response struture is missing the 'prompt' field.")
        # Decompose the response into system and user prompts
        prompts = api_response_data["prompt"] 
        up = [p["content"] for p in prompts if p["role"] == "user"]
        sp = [p["content"] for p in prompts if p["role"] == "system"]
        if len(up) > 1 or len(sp) > 1:
            raise RuntimeError("Retrieved Langfuse prompt [{self.prompt_name}] from API but it had too many 'messages'.  We can accept at most one user and one system message.")
        if len(up) == 0:
            raise RuntimeError("Retrieved Langfuse prompt [{self.prompt_name}] from API but it had no user message.  We require at least one user message.")
        self._cached_prompt = (sp[0].strip() if sp else None, up[0].strip() if up else None)
        self.version = api_response_data["version"]
        self.labels = api_response_data["labels"]
        self.tags = api_response_data["tags"]
        self.updated_at = api_response_data["updatedAt"]
        # Scan both the user and system prompts for variable names appearing using jinja2 template syntax.  Beware of dupes.
        merge_var_names = set()
        for prompt in (self._cached_prompt[0], self._cached_prompt[1]):
            if prompt:
                merge_var_names.update(re.findall(r'{{\s*(\w+)\s*}}', prompt))
        self.var_names = list(merge_var_names)    

    def has_changed(self,sys_prompt:str,user_prompt:str) -> bool:
        """Check if the prompt has changed as compared to the provided values, disregarding leasing and trailing spaces.."""
        return ((sys_prompt or '').strip(), (user_prompt or '').strip()) != self.prompts

    @property
    def prompts(self, allow_cache: bool = True) -> Tuple[str,str]:
        """Returns the system and user prompts as a tuple of strings."""
        if allow_cache and self._cached_prompt is not None:
            return self._cached_prompt
        
        self._cached_prompt = self.fetch_prompts()
        return self._cached_prompt



class _PromptFileWriter:
    """
    Used internally to take new langfuse data and update a prompt file.

    Note that prompt is written with prompty style, prompt at the end of YAML dict format
    
    """
    VOLATILE_PROMPT_RECHECK_MINUTES = 5 # prompts that are not "stable" will be rechecked when they are this many minutes old
    STABLE_PROMPT_RECHECK_DAYS = 1 # prompts that are considered stable will be rechecked when they are this many days old

    HOURS_TO_CONSIDER_PROMPT_STABLE = 1


    def __init__(self, target_file:str,
                 prompt_obj: Optional[SimpleLLMPrompt] = None,
                 langfuse_retriever: Optional[LangfusePromptRetriever] = None, 
                 volatile_prompt_recheck_minutes: Optional[int] = None,
                 stable_prompt_recheck_days: Optional[int] = None
                ):
        """
        Bind to the sources and targets but do not yet write or retrieve.
        
        This relies upon and modifies the prompt_obj metadata, particularly a key it creates/updates named 'langfuse_info'.

        You can manually set the langfuse retriever for things like automated testing or let it be retrieved from the prompt_obj.
        """
        self.prompt_file = target_file
        self.langfuse_retriever = langfuse_retriever
        self.prompt_obj:SimpleLLMPrompt = prompt_obj or SimpleLLMPrompt.from_file(target_file, lazy=False)
        self.volatile_prompt_recheck_minutes = volatile_prompt_recheck_minutes or _PromptFileWriter.VOLATILE_PROMPT_RECHECK_MINUTES
        self.stable_prompt_recheck_days = stable_prompt_recheck_days or _PromptFileWriter.STABLE_PROMPT_RECHECK_DAYS

        if not self.prompt_file.endswith('.yaml'):
            raise ValueError("Prompt file target must have '.yaml' extension.")
        
    def needs_langfuse_update(self,err_on_langfuse_fail:bool = True) -> bool:
        """
        Using the information from the prompt_obj and the langfuse_data, determine if the prompt file needs updating.

        First check the prompt_obj metadata to see:
            - Does it have a langfuse key?  
            - Is the current os.getenv('ENV_TYPE'] in the list of env_types that are allowed to retrieve langfuse data?
            - Has 'langfuse_info' been cleared/deleted? (automatically triggers retrieve)
            - Examine the mdate on the file to see if it is stable or volatile.
               - Determine whether the mdate is old enough to trigger a Langfuse recheck

        If those pass, check the langfuse data:
            - Is the langfuse API's info about prompt 'updated_at' field different from the file?
        """
        if not self.prompt_file:
            return False
        if not os.path.exists(self.prompt_file):
            return True  # doesn't exist.... of course it needs updating
        if 'langfuse' not in self.prompt_obj or os.getenv('ENV_TYPE') not in ENV_TYPE_TO_POLL_LANGFUSE:
            return False
        if 'langfuse_info' not in self.prompt_obj or 'updated_at' not in self.prompt_obj['langfuse_info']:
            return True
        minutes_since_update = (time.time() - os.path.getmtime(self.prompt_file)) / 60
        freshness_threshold = self.volatile_prompt_recheck_minutes if minutes_since_update < self.HOURS_TO_CONSIDER_PROMPT_STABLE * 60 else self.stable_prompt_recheck_days * 24 * 60
        if minutes_since_update < freshness_threshold:
            return False
        if not self.langfuse_retriever:
            try:
                self.langfuse_retriever = LangfusePromptRetriever(self.prompt_obj['langfuse'])
                self.langfuse_retriever.prompts  # force a cache load
            except Exception as e:
                e.add_note("Failed when attempting to check Langfuse for newer version of prompt with Langfuse name {self.prompt_obj['langfuse']}.  You can comment out the 'langfuse' key in the prompt file to disable this check.")
                raise  # re-raise the error
        return self.langfuse_retriever.updated_at != self.prompt_obj['langfuse_info']['updated_at']
    

    def write_prompt_file(self,ostream:TextIO) -> None:
        """
        Write the prompt config file.  Note that the file is written in YAML format, prompty style.
        Uses the langfuse data that was passed in (or dynamically retrieved).

        Note:  This function can be used to dump a prompt, regardless of whether it has Langfuse info or not.
        But, if you are doing this because of langfuse changes, you may want to call needs_langfuse_update() first.
        """
        if not self.langfuse_retriever and 'langfuse' in self.prompt_obj:
            try:
                self.langfuse_retriever = LangfusePromptRetriever(self.prompt_obj['langfuse'])
                self.langfuse_retriever.prompts  # force a cache load
            except Exception as e:
                e.add_note("Failed when attempting to update prompt file with Langfuse prompt name {self.prompt_obj['langfuse']}.  You can comment out the 'langfuse' key in the prompt file to disable this check.")
                raise

        #POSSIBLE FUTURE: Consider a writing strategy that tries to preserve comments in the file??
        to_write = self.prompt_obj.as_dict()
        # strip out any key in the USER_PROMPT_KEYS or SYS_PROMPT_KEYS and 'langfuse_info'
        for key in itertools.chain(USER_PROMPT_KEYS, SYS_PROMPT_KEYS, ['langfuse_info']):
            to_write.pop(key, None)
        # convert the mtime into an iso8601 string
        if self.langfuse_retriever:
            self.langfuse_retriever.prompts  # force retrieval if it hasn't already happened
            mtime_str = strftime('%Y-%m-%dT%H:%M:%S', gmtime(os.path.getmtime(self.prompt_file))) + 'Z'
            to_write['langfuse_info'] = {
                        'updated_at': self.langfuse_retriever.updated_at,
                        'version': self.langfuse_retriever.version,
                        'labels': self.langfuse_retriever.labels,
                        'tags': self.langfuse_retriever.tags,
                        'project_name': self.langfuse_retriever.project_name,   
                        'retrieved_at_local_time': mtime_str,
                      }
        prompts = self.langfuse_retriever.prompts if self.langfuse_retriever else [self.prompt_obj.sys_prompt, self.prompt_obj.user_prompt]
        if prompts[0]:
            prompts = self.langfuse_retriever.prompts if self.langfuse_retriever else [self.prompt_obj.sys_prompt(), self.prompt_obj.user_prompt()]
            if not prompts[0]:
                prompts = prompts[1:]
        ostream.write(yaml.dump(to_write, sort_keys=False,default_flow_style=False).encode('utf-8'))
        if prompts:
            ostream.write('\n#Prompts (sys_prompt and user_prompt appear below separated by triple dash.'.encode('utf-8'))
        for prompt in prompts:
            ostream.write('\n---\n'.encode('utf-8'))
            ostream.write(prompt.encode('utf-8'))
        ostream.write('\n---\n'.encode('utf-8'))  # trailing terminator
#
# End of _PromptFileUpdater class
#
#######################################################    



#######################################################
#
# Command line handling below
#

# Add command line handling using the click library

# Custom representer to enforce block style for all strings
def block_style_presenter(dumper: yaml.Dumper, data: str):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')


# First positional argument is the file glob pattern
@click.command(help="Read a prompt file and display the prompt(s). If file provided, be sure to enclose in quotes.  If no file is provided, read from stdin until EOF or a line with only '---'.")
@click.argument('file_glob', type=str, required=False,)
def main(file_glob: Optional[str]):

    from totodev_pub.app_config_root_class import AppConfigRootClass
    cfg = AppConfigRootClass.dynaload(__file__) # load global configs
    os.environ['ENV_TYPE'] = cfg['ENV_TYPE']

    if file_glob:
        prompt = SimpleLLMPrompt.from_file(file_glob,lazy=False)
        pwriter = prompt.source_file and _PromptFileWriter(prompt.source_file,prompt_obj=prompt)
        if pwriter.needs_langfuse_update(err_on_langfuse_fail=True):
            print(f"Langfuse contains newer data than the file.  Retrieving Langfuse version.")
        pwriter.write_prompt_file(sys.stdout.buffer)
        print(f"\n---\nMERGE VAR NAMES: \n {[('{{' + nm + '}}') for nm in prompt.infer_var_names()]}")
    else:
        print("Enter a prompt followed by '---' on a line by itself.", file=sys.stderr)
        lines = []
        for line in sys.stdin:
            if line.strip() == '---':
                break
            lines.append(line)
        data = {'prompt': ''.join(lines)}
        # write the data to stdout as YAML without wrapping.
        yaml.add_representer(str, block_style_presenter) # Register the custom representer for all strings
        print(yaml.dump(data, default_flow_style=False))

if __name__ == "__main__":
    main()