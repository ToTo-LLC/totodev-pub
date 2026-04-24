# Dependency Strategy for Public Distribution

## Scope and Intent

This document defines the production dependency model for `totodev_pub` with a progressive-install design:

- base install stays lightweight and predictable
- optional ecosystems are opt-in via extras
- import boundaries keep optional stacks isolated from core paths
- CI verifies realistic installation profiles, not only "full environment" installs

## 1) Core Promise Definition

`pip install totodev-pub` guarantees:

- `import totodev_pub` succeeds with no optional ecosystems installed
- core data/config/file primitives are usable (`pydantic`, YAML helpers, file-locking, local storage primitives)
- optional features (LLM, cloud connectors, LucidSpark graph tooling, Luigi-specific pipelines) are not required unless those modules are imported

Non-goal for core install: making every module importable without extras.

## 2) Feature Grouping Proposal

Feature groups are organized by user intent (what a user wants to do), not by internal layering:

- `pipes`: Luigi-based workflow/pipeline execution
- `lucidspark`: graph/tree parsing and board interpretation
- `connectors`: Google/MS365/HTTP/SSH connector ecosystems
- `llm`: language-model protocols, providers, and prompt tooling
- `git`: Git-backed helpers
- `sweep`: pathspec-driven repository sweep utilities
- `logging`: colored logging formatter support
- `all`: all runtime extras
- `dev`: development and test tooling

Rationale:

- users can install a narrow capability set quickly
- extras map cleanly to README examples and error guidance
- maintainers can reason about ownership and release impact per feature area

## 3) Dependency Classification Matrix

| Dependency | Classification | Feature Area | Why |
|---|---|---|---|
| `pydantic` | Core | Data models | Fundamental model/validation layer used broadly. |
| `pyyaml` | Core | Serialization | YAML read/write appears across core and support modules. |
| `portalocker` | Core | File safety | Core file-mapped model locking behavior depends on this. |
| `click` | Core | CLI and command helpers | Referenced by utility modules shipped in base package paths. |
| `python-dotenv` | Core | Config loading | Used by config root utilities. |
| `sqlitedict` | Core | Local cache storage | Used by local persistence helpers. |
| `tomli-w` | Core | TOML writing | Used for TOML save paths in file data utilities. |
| `xxhash` | Core | Cache performance | Used by storage manager hashing paths. |
| `luigi` | Extra | `pipes` | Needed for Luigi-backed pipeline modules only. |
| `networkx` | Extra | `lucidspark` | Graph manipulation for LucidSpark tooling. |
| `treelib` | Extra | `lucidspark` | Tree projections for LucidSpark tooling. |
| `requests` | Extra | `connectors`, `llm` | HTTP integrations for external providers/services. |
| `aiohttp` | Extra | `connectors` | Async connector transport stack. |
| `aiofiles` | Extra | `connectors` | Async file IO in connector paths. |
| `google-auth` | Extra | `connectors`, `llm` | Google identity and auth surfaces. |
| `google-api-python-client` | Extra | `connectors` | Gmail/Sheets API access. |
| `google-auth-oauthlib` | Extra | `connectors` | OAuth browser/device auth support. |
| `google-auth-httplib2` | Extra | `connectors` | Google transport integration for API clients. |
| `msal` | Extra | `connectors` | Azure/MS365 token and auth flow support. |
| `paramiko` | Extra | `connectors` | SSH plugin support in connection tests. |
| `jinja2` | Extra | `llm` | Template rendering in LLM prompt construction. |
| `json-repair` | Extra | `llm` | Best-effort malformed JSON recovery in LLM outputs. |
| `langchain` | Extra | `llm` | Core LangChain APIs. |
| `langchain-community` | Extra | `llm` | Community provider integrations. |
| `langchain-core` | Extra | `llm` | Core LC abstractions used by LLM modules. |
| `langchain-google-genai` | Extra | `llm` | Gemini provider integration. |
| `nest-asyncio` | Extra | `llm` | Event-loop patching in LLM workflows. |
| `openai` | Extra | `llm` | OpenAI client usage in assistant sync paths. |
| `GitPython` | Extra | `git` | `git` module import source for versioning helpers. |
| `pathspec` | Extra | `sweep` | Ignore/path matching support for sweep utilities. |
| `colorlog` | Extra | `logging` | Optional colorized log formatting. |
| `pytest`, `pytest-asyncio`, `mypy`, `ruff` | Extra | `dev` | Development-only verification toolchain. |

## 4) Import Boundary Guidelines

1. `totodev_pub/__init__.py` must not import optional modules.
2. Any module that requires an extra-only dependency must either:
   - keep that import inside function/method scope, or
   - catch `ImportError` at module load and raise a normalized actionable error at feature entry points.
3. Error messages should always include:
   - what dependency is missing
   - which extra to install (preferred)
   - fallback explicit pip command
4. Core modules must not import from optional feature trees (`llm`, connector proxies, LucidSpark) at top level.
5. New optional integrations must include at least one smoke test that validates graceful failure messaging when the extra is absent.

## 5) User-Facing Install UX

README guidance should consistently show:

- `pip install totodev-pub` for baseline workflows
- `pip install "totodev-pub[<extra>]"` for specific capability areas
- concise module-to-extra mapping (for discoverability)
- recommendation to prefer narrow extras over `all`

Install hints from runtime errors should match README naming exactly.

## 6) Verification Strategy (CI/Test Matrix)

CI should include these installation profiles:

1. **Core-only**
   - install: `pip install -e .`
   - verify: `python -c "import totodev_pub"`
   - run: core smoke tests only
2. **Per-extra profile jobs**
   - install: `pip install -e ".[pipes]"` (repeat per extra)
   - verify: import smoke for each extra-owned module family
3. **All-extras profile**
   - install: `pip install -e ".[all]"`
   - verify: full test suite

In each job:

- fail fast on import errors
- report exactly which profile failed
- keep profile boundaries explicit (do not reuse a warmed fully-loaded env for core checks)

## 7) Change Management Policy

When adding or changing dependencies:

1. **Default action:** add to an existing extra, not core.
2. **Core admission criteria:** dependency is required by core promise and appears in common baseline workflows.
3. **Move from extra -> core:** treat as release-note-worthy change; evaluate conflict risk and install-size impact.
4. **Move from core -> extra:** potentially breaking for users who relied on implicit availability; treat as SemVer major unless strong compatibility bridge is provided.
5. **Renaming/removing extras:** SemVer major.
6. **Every dependency PR must include:**
   - matrix row update in this document
   - README install mapping update (if user-facing)
   - CI profile update and passing evidence

## Implementation Applied in This Revision

This strategy has been applied by:

- moving heavy/specialized runtime dependencies out of core into extras in `pyproject.toml`
- adding named optional groups aligned to user tasks
- updating README install docs and module-to-extra guidance
