# totodev_pub

`totodev_pub` is a Python utility library for building pragmatic application tooling, file-backed workflows, small data-processing systems, and integration helpers.

This source snapshot is being prepared for public release under the MIT license. The intended public Python package name is `totodev_pub`. Internal users migrating from the private `totodev_pub` source should plan on an explicit import change and normal regression testing.

## What The Library Covers

The library currently spans several broad areas:

- Configuration loading and environment-aware application setup
- File-backed persistence with Pydantic models and local metadata management
- SQLite utilities and lightweight data stores
- Date-based workspace and cache organization helpers
- Pipeline and job orchestration helpers
- LLM integration helpers and throttling utilities
- Sync and cache abstractions for external systems
- Developer tooling and connectivity-testing utilities

## Representative Modules

- `app_config_root_class.py`: dynamic configuration discovery and environment handling
- `dbjig.py`: SQLite-oriented utility helpers
- `file_mapped_pydantic_mixin.py`: file-backed Pydantic persistence helpers
- `date_folders.py`: date-structured directory management (preferred for new date-based layouts)
- `cached_file_folders.py`: cache and change-detection primitives
- `llm/`: LLM abstractions and support utilities
- `pipes/`: pipeline helpers for delegated work and state tracking
- `lucidspark/`: LucidSpark export parsing and rendering utilities

Supporting and legacy-adjacent utilities (CLI sweep, flexible argv parsing, older date-tree helpers used by `pipes/`, and similar) live under the `minor/` subpackage (`totodev_pub.minor`). Prefer the modules listed above for new work; import from `totodev_pub.minor` when you intentionally need those pieces.

## Packaging Direction

The original internal library was commonly consumed as source mounted under `src/totodev_pub/`. The public release is targeting the import path `totodev_pub` instead.

That means adopters should expect:

- explicit import updates from `totodev_pub` to `totodev_pub`
- regression testing before switching consumers over
- a public packaging layer that is cleaner than the original internal source layout

## Working With The Source Tree

This review snapshot still reflects the original internal source organization. Some examples and tests are therefore source-layout oriented while the public package structure is being assembled.

The long-term public-facing goals are:

- package/install usage around `totodev_pub`
- examples that use synthetic, non-identifying data
- contributor docs that do not assume an internal company workflow

## Examples And Integrations

Examples are intentionally being retained where possible. Integration-heavy modules may require additional dependencies, credentials, or service-specific setup. Sensitive data should never be committed to the repository; examples should rely on environment variables and synthetic sample inputs.

## Related Notes

- The root MIT license for the public repository is the intended project license.
- LucidSpark examples, cache examples, and connection-testing helpers are being reviewed so they can remain useful without exposing identifying internal information.
