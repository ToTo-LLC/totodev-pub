# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import pytest
import os
import csv
import tempfile
import threading
import time
from unittest.mock import patch, MagicMock
import portalocker

# Import the class we want to test
from totodev_pub.llm.simplified_llm import _LLMRequestLogger

class TestLLMRequestLog:
    """Tests for the _LLMRequestLog class."""
    
    def test_init(self):
        """Test initialization of the _LLMRequestLog class."""
        # Test with a file path
        log = _LLMRequestLogger("test.csv")
        assert log.log_file == "test.csv"
        assert log.allow_queued_writes == 10
        assert log.queued_requests == []
        
        # Test with None as file path
        log = _LLMRequestLogger(None)
        assert log.log_file is None
        
        # Test with custom queue size
        log = _LLMRequestLogger("test.csv", allow_queued_writes=20)
        assert log.allow_queued_writes == 20
    
    def test_log_request_none_file(self):
        """Test that logging is a no-op when log_file is None."""
        log = _LLMRequestLogger(None)
        # This should not raise any exceptions
        log.log_request("test", "gpt-4", 100, 1.5)
        assert log.queued_requests == []
    
    def test_log_request_basic(self):
        """Test basic logging functionality."""
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
        
        try:
            log = _LLMRequestLogger(temp_path)
            log.log_request("test_label", "gpt-4", 100, 1.5)
            
            # Verify file contents
            with open(temp_path, 'r') as f:
                reader = csv.reader(f)
                rows = list(reader)
                
                # Should have header and one data row
                assert len(rows) == 2
                assert rows[0] == _LLMRequestLogger.HEADER
                assert rows[1][1:] == ["test_label", "gpt-4", "100", "1.5"]
        finally:
            # Clean up
            if os.path.exists(temp_path):
                os.remove(temp_path)
    
    def test_log_request_append(self):
        """Test that logging appends to an existing file."""
        # Create a file with header and one row
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_file:
            writer = csv.writer(temp_file)
            writer.writerow(_LLMRequestLogger.HEADER)
            writer.writerow(["2023-01-01T00:00:00", "existing", "gpt-3", "50", "0.5"])
            temp_path = temp_file.name
        
        try:
            log = _LLMRequestLogger(temp_path)
            log.log_request("new_entry", "gpt-4", 100, 1.5)
            
            # Verify file contents
            with open(temp_path, 'r') as f:
                reader = csv.reader(f)
                rows = list(reader)
                
                # Should have header and two data rows
                assert len(rows) == 3
                assert rows[0] == _LLMRequestLogger.HEADER
                assert rows[1][1:] == ["existing", "gpt-3", "50", "0.5"]
                assert rows[2][1:] == ["new_entry", "gpt-4", "100", "1.5"]
        finally:
            # Clean up
            if os.path.exists(temp_path):
                os.remove(temp_path)
    
    def test_queued_writes(self):
        """Test that writes are queued when file is locked."""
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
        
        try:
            # Mock portalocker to simulate a locked file
            with patch('portalocker.Lock', side_effect=portalocker.LockException):
                log = _LLMRequestLogger(temp_path, allow_queued_writes=3)
                
                # First log should be queued
                log.log_request("test1", "gpt-4", 100, 1.5)
                assert len(log.queued_requests) == 1
                
                # Second log should be queued
                log.log_request("test2", "gpt-4", 200, 2.5)
                assert len(log.queued_requests) == 2
                
                # Third log should be queued
                log.log_request("test3", "gpt-4", 300, 3.5)
                assert len(log.queued_requests) == 3
                
                # Fourth log should raise an exception
                with pytest.raises(Exception) as excinfo:
                    log.log_request("test4", "gpt-4", 400, 4.5)
                assert "queue exceeded maximum size" in str(excinfo.value)
        finally:
            # Clean up
            if os.path.exists(temp_path):
                os.remove(temp_path)
    
    def test_retry_success(self):
        """Test that queued writes are written when lock is released."""
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
        
        # Create a mock that fails once then succeeds
        original_lock = portalocker.Lock
        mock_calls = [0]  # Use a list to track calls
        
        def mock_lock_side_effect(*args, **kwargs):
            mock_calls[0] += 1
            if mock_calls[0] == 1:
                raise portalocker.LockException("Locked")
            return original_lock(*args, **kwargs)
        
        try:
            with patch('portalocker.Lock', side_effect=mock_lock_side_effect):
                log = _LLMRequestLogger(temp_path)
                
                # First attempt should fail and queue
                log.log_request("test1", "gpt-4", 100, 1.5)
                
                # Second attempt should succeed and write both entries
                log.log_request("test2", "gpt-4", 200, 2.5)
                
                # Verify file contents
                with open(temp_path, 'r') as f:
                    reader = csv.reader(f)
                    rows = list(reader)
                    
                    # Should have header and two data rows
                    assert len(rows) == 3
                    assert rows[0] == _LLMRequestLogger.HEADER
                    assert rows[1][1:] == ["test1", "gpt-4", "100", "1.5"]
                    assert rows[2][1:] == ["test2", "gpt-4", "200", "2.5"]
        finally:
            # Clean up
            if os.path.exists(temp_path):
                os.remove(temp_path)
    
    def test_concurrent_writes(self):
        """Test concurrent writes to the log file."""
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
        
        try:
            # Create multiple threads to write to the log file
            log = _LLMRequestLogger(temp_path)
            threads = []
            
            def log_request(idx):
                log.log_request(f"test{idx}", "gpt-4", idx * 100, idx * 0.5)
            
            # Create and start 5 threads
            for i in range(5):
                thread = threading.Thread(target=log_request, args=(i,))
                threads.append(thread)
                thread.start()
            
            # Wait for all threads to complete
            for thread in threads:
                thread.join()
            
            # Verify file contents
            with open(temp_path, 'r') as f:
                reader = csv.reader(f)
                rows = list(reader)
                
                # In concurrent writes, we might get multiple headers
                # Filter out header rows and check that we have 5 data rows
                data_rows = [row for row in rows if row[0] != "timestamp"]
                assert len(data_rows) == 5
                
                # Check that all entries were written (order may vary)
                labels = [row[1] for row in data_rows]
                assert set(labels) == {"test0", "test1", "test2", "test3", "test4"}
        finally:
            # Clean up
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
    def test_directory_creation(self):
        """Test that directories are created if they don't exist."""
        # Create a temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a path to a non-existent subdirectory
            subdir = os.path.join(temp_dir, "subdir1", "subdir2")
            log_path = os.path.join(subdir, "test_log.csv")
            
            # Verify the directory doesn't exist yet
            assert not os.path.exists(subdir)
            
            # Create the log and write to it
            log = _LLMRequestLogger(log_path)
            log.log_request("test_label", "gpt-4", 100, 1.5)
            
            # Verify the directory and file were created
            assert os.path.exists(subdir)
            assert os.path.exists(log_path)
            
            # Verify file contents
            with open(log_path, 'r') as f:
                reader = csv.reader(f)
                rows = list(reader)
                
                # Should have header and one data row
                assert len(rows) == 2
                assert rows[0] == _LLMRequestLogger.HEADER
                assert rows[1][1:] == ["test_label", "gpt-4", "100", "1.5"] 
                
    def test_flush_success(self):
        """Test that flush successfully writes queued entries."""
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
        
        try:
            # Create a logger and simulate queued entries
            log = _LLMRequestLogger(temp_path)
            
            # Manually add entries to the queue
            import datetime
            timestamp1 = datetime.datetime.now().isoformat()
            timestamp2 = datetime.datetime.now().isoformat()
            log.queued_requests = [
                [timestamp1, "queued1", "gpt-4", "100", "1.5"],
                [timestamp2, "queued2", "gpt-4", "200", "2.5"]
            ]
            
            # Flush the queue
            log.flush()
            
            # Verify queue is empty
            assert log.queued_requests == []
            
            # Verify file contents
            with open(temp_path, 'r') as f:
                reader = csv.reader(f)
                rows = list(reader)
                
                # Should have header and two data rows
                assert len(rows) == 3
                assert rows[0] == _LLMRequestLogger.HEADER
                assert rows[1][1:] == ["queued1", "gpt-4", "100", "1.5"]
                assert rows[2][1:] == ["queued2", "gpt-4", "200", "2.5"]
        finally:
            # Clean up
            if os.path.exists(temp_path):
                os.remove(temp_path)
    
    def test_flush_failure(self):
        """Test that flush raises an exception when it can't write the queue."""
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
        
        try:
            # Create a logger and simulate queued entries
            log = _LLMRequestLogger(temp_path)
            
            # Manually add entries to the queue
            import datetime
            timestamp = datetime.datetime.now().isoformat()
            log.queued_requests = [
                [timestamp, "queued1", "gpt-4", "100", "1.5"]
            ]
            
            # Mock portalocker to always fail
            with patch('portalocker.Lock', side_effect=portalocker.LockException):
                # Flush should raise an exception after max_attempts
                with pytest.raises(Exception) as excinfo:
                    log.flush(max_attempts=2)
                
                # Verify the exception message
                assert "Failed to flush" in str(excinfo.value)
                
                # Verify queue still contains the entry
                assert len(log.queued_requests) == 1
        finally:
            # Clean up
            if os.path.exists(temp_path):
                os.remove(temp_path) 