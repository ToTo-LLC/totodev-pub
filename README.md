# totodev_pub

`totodev_pub` is a Python utility library being prepared for public release under the MIT license.

The project is intended to become the public home for a broad snapshot of the internal `totodev_pub` library, with identifying/internal material removed and examples rewritten to use safe synthetic data where needed.

## Organization

This project is maintained by [TomorrowToday LLC](https://tomorrowtoday.com).

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
- `logging`: colorized logging helpers
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

### Module-to-Extra Guidance

- `totodev_pub.lucidspark.*` -> `lucidspark`
- `totodev_pub.llm.*` -> `llm`
- `totodev_pub.cached_file_folders_support.file_proxy_gmail` -> `connectors`
- `totodev_pub.cached_file_folders_support.file_proxy_outlook_email` -> `connectors`
- `totodev_pub.cached_file_folders_support.file_proxy_sharepoint` -> `connectors`
- `totodev_pub.cli.conn_tester_support.test_plugins.conntest_ssh` -> `connectors`
- `totodev_pub.pipes.*` (Luigi paths) -> `pipes`

Detailed rationale and governance policy are documented in `docs/dependency-strategy.md`.
