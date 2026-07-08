# Proposed: Remove Langchain from SimplifiedLLM class

- Proposed 2025-11-10

## Motivation

- Heavy dependency: `SimplifiedLLM` pulls in large portions of LangChain but only exercises the lightest abstractions (chat model wrappers, prompt helpers, JSON parsing). This adds import-time cost, transitive dependency weight, and configuration complexity for every project that relies on `SimplifiedLLM`.
- Python 3.12 holdback: LangChain’s release cadence has lagged behind the latest Python versions; some deployments may remain pinned to 3.11 until their compatibility issues clear, blocking upgrades in downstream services.
- Project agility: Many teams ship small LLM-backed utilities. Carrying LangChain everywhere discourages experimentation with leaner stacks and complicates maintenance for teams that just want straightforward chat completions.

## Alternatives Considered

1. **Introduce a new any-llm–backed class (recommended)**  
   - Ship an `AnySimplifiedLLM` (working name) that uses any-llm internally, keep the existing `SimplifiedLLM` intact but mark it deprecated, and guide new projects toward the modern facade. This limits regression risk while giving us room to redesign the API around lessons learned.
2. **Dual implementation behind a runtime flag**  
   - Retain the current class name but inject a strategy layer that selects LangChain or any-llm based on a global setting. This preserves public contracts but doubles the code paths and test matrices; it defers Python 3.12 adoption until the new backend is proven for all scenarios.
3. **Hard in-place replacement**  
   - Swap LangChain out for any-llm inside `SimplifiedLLM` in one step. This achieves the dependency and Python goals immediately but requires exhaustive regression coverage and likely introduces breaking changes for callers that depend on LangChain-specific behavior.

We recommend option 1: create a new, any-llm–centric facade while deprecating the old class. It offers a safe migration runway, isolates the refactor from legacy constraints, and lets early adopters validate the new implementation before we consider sunsetting the legacy class.

## Current LangChain Footprint

- **Protocol factories:** `totodev_pub.llm.base_protocols` registers `azure`, `openai`, `huggingface`, `google`, and `fake` makers that instantiate `AzureChatOpenAI`, `ChatOpenAI`, `HuggingFaceEndpoint`, and `ChatGoogleGenerativeAI` from LangChain community packages.
- **Core execution path:** `SimplifiedLLM._answer` assumes the returned objects implement LangChain’s `BaseChatModel` / `BaseLLM` contracts. It builds `ChatPromptTemplate` instances, calls `.bind(response_format=...)`, executes `.ainvoke`, and normalizes `AIMessage` / `Generation` responses. JSON responses are parsed through `JsonOutputParser`, wrapped by our `RepairingJsonOutputParser`.
- **Testing and shims:** `FakeLLM` mimics a LangChain `BaseLLM` for unit tests, and legacy convenience methods (`register_openai_llm_maker`, `register_azure_llm_maker`, etc.) forward to PKD-driven registration that still yields LangChain objects.
- **Ancillary tooling:** Throttling, caching, and logging layers are LangChain-agnostic but expect the underlying model to be coroutine-friendly and to accept LangChain-style call patterns.

## any-llm Fit Analysis

- **Feature overview:** any-llm is a lightweight provider-agnostic wrapper published by Mozilla AI. It exposes a single `AnyLLM` interface that can target OpenAI, Azure OpenAI, Anthropic, Google, Hugging Face, AWS Bedrock, and more using provider-specific credential dictionaries. Async and sync chat-completion helpers are available, and raw provider kwargs can be passed through when necessary.
- **Re-implementing `SimplifiedLLM`:**
  - Protocol makers would instantiate `AnyLLM(provider="openai", api_key=..., model="...")` (or the relevant provider) instead of LangChain chat models. Our PKD parsing remains useful for assembling the option dictionaries.
  - `_answer` would construct role-based message lists and call `await any_llm.chat(messages=[...], **kwargs)` (or the equivalent low-level method). We would implement the small amount of prompt templating we currently delegate to `ChatPromptTemplate`.
  - Structured outputs would rely on provider-native JSON modes where available (OpenAI response_format, Azure mirrors, Anthropic tool use) and fall back to our `RepairingJsonOutputParser`. We may need per-provider routing because any-llm does not normalize `response_format` yet.
  - Streaming and advanced LangChain-specific hooks (e.g., LCEL pipelines, `.bind`) would need shims or be dropped; we should inventory external callers to confirm they do not depend on them.
  - `FakeLLM` would be rewritten to conform to the any-llm interface (likely a simple class with `.chat` returning canned responses). Existing throttling, caching, and logging modules can wrap the new adapter with minimal change.

This analysis suggests any-llm offers a solid base for a lean successor to `SimplifiedLLM`. A fresh class lets us define a tighter surface area while verifying that the essential behaviors (async invocations, optional JSON parsing, throttle integration) remain intact.

## Prep Required

- Finalize the migration path choice (baseline assumption: introduce `AnySimplifiedLLM`) and capture the deprecation plan for the LangChain-backed class.
- Identify enhancements we want in the new facade beyond parity: clearer registration ergonomics, explicit provider JSON-mode support, better test doubles, optional streaming hooks, and formal Python 3.12 support.
- Survey downstream consumers of `SimplifiedLLM` to prioritize early adopters and gather requirements that should shape the `AnySimplifiedLLM` backlog.