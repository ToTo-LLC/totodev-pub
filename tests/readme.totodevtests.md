# Test Layout Notes

These tests were originally written for a source-vendored copy of the library, which is why they live alongside the library modules rather than inside an application-specific test tree.

As the public package layout around `totodev_pub` is finalized, some paths and import examples may continue to evolve. The tests remain useful as regression coverage for the underlying library behavior.

## Optional-dependency tests

Some tests exercise feature areas that depend on optional extras (`pipes`,
`connectors`, `lucidspark`, `llm`, `git`). These tests must not break a
core-only environment. Two mechanisms keep them safe:

1. Centralized collection gating in `conftest.py`. The `_OPTIONAL_TEST_RULES`
   table maps a feature marker and a path predicate to the importable modules a
   test needs. When any required module is missing, the test is skipped at
   collection (no `ModuleNotFoundError`), and matching tests are auto-tagged with
   their feature marker so the core/full lanes can select them with `-m`.
2. A module-level `pytest.importorskip("<module>")` guard placed before any
   top-level import of the optional package, as defense-in-depth for direct runs.

When adding an optional-dependency test:

- add (or extend) a rule in `_OPTIONAL_TEST_RULES` in `conftest.py`
- guard top-level optional imports with `pytest.importorskip(...)`
- confirm the marker name matches the extra in `pyproject.toml` and the marker
  registered in `pytest.ini`

See the "Testing Profiles" section of the top-level `README.md` for the core and
full lane commands.
