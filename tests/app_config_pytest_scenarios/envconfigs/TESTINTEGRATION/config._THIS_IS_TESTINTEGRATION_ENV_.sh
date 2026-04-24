#!/bin/bash
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

# Environment configuration for integration tests (with external services)

export TEST_MODE="true"
export USE_MOCK_DATABASE="false"
export TEST_DATABASE_URL="postgresql://localhost/test_integration_db"
export TEST_API_KEY="integration_test_key_12345"
export EXTERNAL_SERVICE_URL="http://localhost:8888"
export LOG_LEVEL="INFO"
export INTEGRATION_VALUE="integration_test_value"

