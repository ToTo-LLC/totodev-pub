# totodev_pub

`totodev_pub` is a Python utility library being prepared for public release under the MIT license.

The project is intended to become the public home for a broad snapshot of the internal `totodev_pub` library, with identifying/internal material removed and examples rewritten to use safe synthetic data where needed.

## Organization

This project is maintained by [TomorrowToday LLC](https://tomorrowtoday.com).

## Documentation

| Document | Covers |
|---|---|
| [`docs/case-manager-deployment.md`](docs/case-manager-deployment.md) | Running a `CaseManager` in production: the three layers, the exit-code contract, supervisor setup (systemd, Compose, Kubernetes, supervisord), shutdown, the watchdog, and container caveats |
| [`docs/case-manager-layout.md`](docs/case-manager-layout.md) | The on-disk layout a manager provisions — generated from the same declaration the code provisions from, so it cannot drift |
| [`examples/README-case_manager.md`](src/totodev_pub/case_manager_support/examples/README-case_manager.md) | Five worked, runnable examples for the manager side: embedded fleet, minimal host, batch runner, request-serving host, request-queue stages |
| [`docs/case-workbench-notebooks.md`](docs/case-workbench-notebooks.md) | The `CaseWorkbench` marimo notebooks |
| [`docs/dependency-strategy.md`](docs/dependency-strategy.md) | Which imports belong to which optional extra, and the governance policy behind that split |

Designing a case *type* is a separate subject with its own tooling — see the
`case-designer` skill under `.claude/skills/`.

## Direction

- Public Python package name: `totodev_pub`
- License: MIT
- Migration from the private library should be explicit and should include regression testing
- Examples and integration helpers are being retained where they can be made safe for publication

## `minor` utilities

Less central or legacy-adjacent pieces of the library are grouped under the `totodev_pub.minor` package (sources in `src/totodev_pub/minor/`). That subdirectory is the home for utilities we still ship but do not treat as primary API surface—examples include the sweep tool, flexible CLI args (`FlexArgs`), and the date-tree folder helpers used by the Luigi `pipes` stack. Prefer imports from `totodev_pub.minor.<module>` for those modules rather than expecting them at the top level of `totodev_pub`.

## Current Working Area

The source review snapshot currently lives under `volatile/repo/totodev_pub`, and planning/review notes live under `volatile/tmp/`.

## Status

This repository is in the middle of public-release preparation work, including:

- concern inventory and review tracking
- depersonalization of docs, examples, and fixtures
- README and contributor-documentation rewrites
- preparation for a clean public package layout around `totodev_pub`

## Install Strategy

The package now uses a progressive dependency model:

- `pip install totodev-pub` installs only the lightweight core
- feature-specific dependencies are installed through extras
- optional features should raise actionable install guidance when extras are missing

### Core Promise

Base install guarantees:

- `import totodev_pub` works without cloud/LLM/graph ecosystems
- core file/config/event-log utilities work with only base dependencies
- optional stacks (LLM, LucidSpark, cloud connectors, etc.) are opt-in

### Feature Extras

- `pipes`: Luigi-backed pipe execution helpers
- `lucidspark`: LucidSpark graph/tree tooling
- `connectors`: Google/MS365/HTTP/SSH connector integrations
- `llm`: LangChain/OpenAI/Gemini-oriented LLM integration utilities
- `git`: Git-backed utilities
- `sweep`: repository sweep/pathspec tooling
- `all`: all runtime extras
- `dev`: development/test/lint dependencies

### Install Examples

```bash
# Core only
pip install totodev-pub

# Single feature area
pip install "totodev-pub[lucidspark]"

# Common integration profile
pip install "totodev-pub[connectors,llm]"

# Everything runtime-related
pip install "totodev-pub[all]"
```

### Quick Import Example

Use the package name `totodev-pub` for installation, but `totodev_pub` for Python imports:

```python
from totodev_pub.forgetful_reader import ForgetfulReader


def load_user(user_id: int) -> str:
    # Placeholder for a high-cost call (API/database/etc.)
    return f"profile:{user_id}"


cache = ForgetfulReader(
    value_retriever=lambda key: load_user(key[0]),
    expiration_seconds=30,
)

print(cache.get((42,)))  # first call retrieves and caches
print(cache.get((42,)))  # second call uses cache until expiry
```

### Module-to-Extra Guidance

- `totodev_pub.lucidspark.*` -> `lucidspark`
- `totodev_pub.llm.*` -> `llm`
- `totodev_pub.cached_file_folders_support.file_proxy_gmail` -> `connectors`
- `totodev_pub.cached_file_folders_support.file_proxy_outlook_email` -> `connectors`
- `totodev_pub.cached_file_folders_support.file_proxy_sharepoint` -> `connectors`
- `totodev_pub.cli.conn_tester_support.test_plugins.conntest_ssh` -> `connectors`
- `totodev_pub.pipes.*` (Luigi paths) -> `pipes`

Detailed rationale and governance policy are documented in `docs/dependency-strategy.md`.

## Testing Profiles

The test suite mirrors the progressive dependency model: optional-feature tests are
gated so a minimal install never fails at collection time.

### Core lane (minimal install)

```bash
uv sync --extra dev
PYTHONPATH=src pytest -m "not pipes and not connectors and not lucidspark and not llm and not git"
```

This protects the core-install contract. Tests that need an optional stack are
automatically skipped at collection when their dependency is missing (see
`tests/conftest.py`), so a plain `PYTHONPATH=src pytest` also runs cleanly in a
core environment.

### Full lane (all extras)

```bash
uv sync --extra dev --extra all
PYTHONPATH=src pytest
```

This installs every runtime extra and runs the entire suite, exposing real
regressions across all feature areas.

### Before tagging a release: clear the lazy-test cache

Tests wrapped in `very_lazy_test` (see `src/totodev_pub/pytest_tools.py`) skip
themselves on a later run once they've passed, recording that pass in a gitignored
`tests/<name>.passed_tests.tmp` file — a deliberate cost-saving measure for iterative
local development, not a correctness gap. On a machine with accumulated cache files
this can silently skip several dozen tests even in the "full lane" above, so a normal
green run during day-to-day work is **not sufficient evidence for a release**.

Before tagging, clear the cache and run once with nothing eligible to skip:

```bash
rm -f tests/*.passed_tests.tmp
PYTHONPATH=src pytest
```

Compare the passed/skipped counts against a cached run from the same tree if you want
to confirm the cache was actually doing something — the difference is exactly the set
of tests a routine local run would not have exercised.

### How optional-feature gating works

- Optional-feature tests are tagged with markers (`pipes`, `connectors`,
  `lucidspark`, `llm`, `git`) that map to the extras in `pyproject.toml`.
- `tests/conftest.py` holds a single data-driven mapping (path -> required
  importable modules). When a required module is missing, the test is skipped at
  collection rather than raising `ModuleNotFoundError`.
- Test modules that import an optional package at module top level also call
  `pytest.importorskip("<module>")` as a local safeguard, so they skip cleanly
  even when run directly.

When adding a test that imports an optional stack, follow the same pattern: add a
rule to `_OPTIONAL_TEST_RULES` in `tests/conftest.py` and guard top-level
optional imports with `pytest.importorskip(...)`.
