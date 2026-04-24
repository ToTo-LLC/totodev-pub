# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
from datetime import timedelta
from unittest.mock import MagicMock
from totodev_pub.forgetful_reader import ForgetfulReader  # Import your class here

class TestForgetfulReader:
    def setup_method(self):
        self.mock_retriever = MagicMock(return_value="test_value")
        self.key = ("test_key",)
        self.reader = ForgetfulReader(self.mock_retriever, expiration_seconds=10)

    def test_initialization(self):
        assert self.reader._value_retriever is not None
        assert self.reader.expiration_seconds == 10

    def test_retrieve_no_cache(self):
        no_cache_reader = ForgetfulReader(self.mock_retriever, 0)
        value = no_cache_reader.retrieve(self.key)
        self.mock_retriever.assert_called_with(self.key)
        assert value == "test_value"

    def test_caching_behavior(self):
        self.reader.retrieve(self.key)
        value = self.reader.get(self.key)
        assert value == "test_value"
        assert self.mock_retriever.call_count == 1  # Ensure retriever is called only once

    def test_expiration_logic(self):
        self.reader.retrieve(self.key)
        self.reader._cache[self.key]['expiration'] -= timedelta(seconds=20)
        assert self.reader._is_expired(self.key)

    def test_override_functionality(self):
        self.reader.override(self.key, "new_value")
        value = self.reader.get(self.key)
        assert value == "new_value"

    def test_cache_expiration_on_retrieve(self):
        self.reader.retrieve(self.key)
        self.reader._cache[self.key]['expiration'] -= timedelta(seconds=20)
        self.reader.retrieve(self.key)
        assert self.mock_retriever.call_count == 2

    def test_compact_functionality(self):
        self.reader.retrieve(self.key)
        self.reader._cache[self.key]['expiration'] -= timedelta(seconds=20)
        self.reader.compact()
        assert self.key not in self.reader._cache

    def test_flush_functionality(self):
        self.reader.retrieve(self.key)
        self.reader.flush()
        assert not self.reader._cache
