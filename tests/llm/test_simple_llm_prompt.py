# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
import os
import json
import tempfile
from typing import Optional
from datetime import datetime
from totodev_pub.llm.simple_llm_prompt import (SimpleLLMPrompt,
                                       LangfusePromptRetriever,
                                       _PromptFileWriter, 
                                       _SimpleLLMPromptImpl, 
                                       LangfusePrompt
                                      )
from pathlib import Path

from totodev_pub.app_config_root_class import AppConfigRootClass

_SCENARIO = Path(__file__).resolve().parent.parent / "app_config_pytest_scenarios"
_ENV_UNIT = _SCENARIO / "envconfigs" / "TESTUNIT"
CONFIG = AppConfigRootClass.dynaload(
    [str(_ENV_UNIT), str(_SCENARIO)],
    require_config_file=False,
)

_PERMITTED_ENV_TYPES = ['*']

@pytest.fixture
def sample_langfuse_prompts_api_response() -> dict:
    s = """{"id":"cm3vsrajw0061elwy0n1t8dtu","createdAt":"2024-11-24T16:11:51.164Z","updatedAt":"2024-11-24T16:11:59.716Z","projectId":"cm3qt3de700vtfcg54ce8hv9y","createdBy":"cm3qsm2qm00vifcg56wa5iw93","prompt":[{"role":"system","content":"This is a stale, offline, dummy system prompt."},{"role":"user","content":"This is a stale, offline, dummy user prompt."}],"name":"dummy_prompt_AAA","version":3,"type":"chat","isActive":null,"config":{},"tags":[],"labels":["latest","production"]}"""
    return json.loads(s)

@pytest.fixture
def dummy_langfuse_prompt_retriever(sample_langfuse_prompts_api_response) -> LangfusePromptRetriever:
    """return a dummy langfuse prompt retriever without contacting langfuse"""
    prompt_name = sample_langfuse_prompts_api_response['name']
    lf_retriever = LangfusePromptRetriever(prompt_name,env_type_allow_prompt_retrieval=_PERMITTED_ENV_TYPES) # allow test to run in any env
    lf_retriever._parse_and_cache_prompt_response(sample_langfuse_prompts_api_response,proj_name="dummy_proj")
    return lf_retriever

@pytest.fixture
def langfuse_configs()->Optional[dict]:
    """return the langfuse credentials."""
    key_map = {'LANGFUSE_SECRET_KEY':'secret_key',
               'LANGFUSE_PUBLIC_KEY':'public_key',
               'LANGFUSE_HOST':'host',
               'LANGFUSE_TEST_PROMPT_NAME':'prompt_name',
              }
    needed_keys = list(key_map.keys())
    if all([key in CONFIG or key in os.environ for key in needed_keys]):
        return {key_map[key]:(CONFIG[key] or os.environ[key]) for key in needed_keys}
    return None  # it's all or nothing on this

@pytest.fixture
def can_contact_langfuse(langfuse_configs:Optional[dict]):
    """return True if can contact langfuse."""
    return langfuse_configs is not None


def ppath(fname:str) -> str:
    """return the full path of the file assuming it is in a directory named 'test_simple_llm_fixtures' underneath the directory of this file."""
    import os
    return os.path.join(os.path.dirname(__file__), 'test_simple_llm_fixtures', fname)


def test_simple_llm_prompt():
    """test the SimpleLLMPrompt class."""
    p1 = SimpleLLMPrompt.from_file(ppath('p001*.yaml'))
    p2 = SimpleLLMPrompt.from_file(ppath('p001*.txt'))
    p3 = SimpleLLMPrompt.from_file(ppath('p001*prompty.yaml'))
    assert p1.render() == p2.render() == p1.render()
    assert p2.render() == p3.render() 
    assert p1['ignored_key'] == 'ignored_value'
    assert p3['ignored_key'] == 'ignored_value'

def test_compound_prompt():
    """case where has both user and sys prompt."""
    p1 = SimpleLLMPrompt.from_file(ppath('p002*.yaml'))
    p2 = SimpleLLMPrompt.from_file(ppath('p002*prompty.yaml'))
    assert p1.render() == p2.render()
    assert isinstance(p1.render(),list)
    assert "You are a math helper." in p1.render()[0]
    assert "What is 1+1?" in p1.render()[1]


def test_langfuse_retrieval(can_contact_langfuse, langfuse_configs: Optional[dict]):
    """test the langfuse retrieval only. Requires actual good credentials and a good prompt."""
    if not can_contact_langfuse or langfuse_configs is None:  # Added check for None configs
        pytest.skip("No langfuse credentials found or configs not available.")
        
    creds = langfuse_configs
    creds['prompt_name'] = "long-elaborate-prompt-name-deleteme01"  # this will eventually be bad and need replace or deactivate
    lf_retriever: LangfusePromptRetriever = LangfusePromptRetriever(**creds)
    lf_retriever.prompts # force retrieval
    assert isinstance(lf_retriever.prompt_name,str)
    assert isinstance(lf_retriever.project_name,str)
    assert len(lf_retriever.prompts) == 2


def test_prompt_file_writer(dummy_langfuse_prompt_retriever, monkeypatch, caplog) -> None:
    """Test that the prompt file writer correctly detects when updates are needed"""
    import logging
    caplog.set_level(logging.DEBUG)
    
    # Set up environment for langfuse updates
    monkeypatch.setenv('ENV_TYPE', 'DEV')  # Ensure we're in a valid env type
    
    src_filepath = ppath('p002-compound_test_prompt.yaml')  # full path
    p_from_file = SimpleLLMPrompt.from_file(src_filepath)
    
    # Create a modified version of the prompt with langfuse info
    guts = p_from_file.as_dict()
    guts['langfuse'] = dummy_langfuse_prompt_retriever.prompt_name
    guts['env_types_allow_prompt_retrieval'] = ['DEV']  # Allow updates in test env
    
    # Test case 1: Missing langfuse_info entirely
    p_test = SimpleLLMPrompt.from_dict(guts)  # Don't add langfuse_info
    
    pfw = _PromptFileWriter(
        src_filepath,
        prompt_obj=p_test,
        langfuse_retriever=dummy_langfuse_prompt_retriever,
        volatile_prompt_recheck_minutes=0,
        stable_prompt_recheck_days=0
    )
    
    assert pfw.needs_langfuse_update(), (
        "Should need langfuse update when:\n"
        "1. Langfuse key is present\n"
        "2. Environment type is allowed\n"
        "3. Langfuse info is missing entirely\n"
        "4. Last check time is expired"
    )

def test_fetch_prompts_with_mock():
    mock_prompt = LangfusePrompt(
        system_content="sys prompt",
        user_content="user prompt",
        version=1,
        labels=["production"],
        tags=["test"],
        updated_at=datetime(2024, 1, 1),
        project_name="test_project"
    )
    
    retriever = LangfusePromptRetriever(
        prompt_name="test_prompt",
        mock_langfuse_prompt=mock_prompt
    )
    
    prompts = retriever.fetch_prompts()
    assert prompts == ("sys prompt", "user prompt")
    assert retriever.version == 1
    assert retriever.labels == ["production"]
    assert retriever.tags == ["test"]
    assert retriever.updated_at == "2024-01-01T00:00:00Z"
    assert retriever.project_name == "test_project"

def test_mock_bypasses_env_check():
    """Verify that using a mock bypasses environment type restrictions"""
    mock_prompt = LangfusePrompt(
        user_content="test prompt",
        version=1,
        updated_at=datetime.now()
    )
    
    # This should work even though ENV_TYPE isn't in allowed list
    retriever = LangfusePromptRetriever(
        prompt_name="test",
        mock_langfuse_prompt=mock_prompt,
        env_type_allow_prompt_retrieval=["DEV"]  # We're not in PROD
    )
    
    prompts = retriever.fetch_prompts()
    assert prompts[1] == "test prompt" 