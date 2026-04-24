#!/bin/bash
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

# Environment configuration for unit tests (no external dependencies)

export TEST_MODE="true"
export USE_MOCK_DATABASE="true"
export MOCK_EXTERNAL_APIS="true"
export LOG_LEVEL="DEBUG"
export TEST_DATA_DIR="tests/fixtures"
export SIMPLE_VALUE="unit_test_value"

