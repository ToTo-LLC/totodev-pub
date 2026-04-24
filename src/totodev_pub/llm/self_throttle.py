# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

import time
import logging
from abc import ABC, abstractmethod
from typing import Optional
from collections import deque
import asyncio
from totodev_pub.dbjig import DbJig

logger = logging.getLogger(__name__)


class _CharUsageLogBase(ABC):
    DEFAULT_CUTOFF_SECS = 60 # used if no cutoff is provided.


    @property
    def default_cutoff_secs(self) -> float:
        # if self._default_cutoff_secs has not been set, set it with the class default
        if not hasattr(self, "_default_cutoff_secs"):
            self._default_cutoff_secs = self.__class__.DEFAULT_CUTOFF_SECS
        return self._default_cutoff_secs

    @default_cutoff_secs.setter
    def default_cutoff_secs(self, value: float):
        if value <= 0:
            raise ValueError("Cutoff seconds must be positive")
        self._default_cutoff_secs = value

    def cur_cutoff(self) -> float:
        return self.cur_timestamp() - self.default_cutoff_secs  

    @abstractmethod
    def cur_timestamp(self) -> float:
        """
        Return the current timestamp in seconds.  
        This may be calculated by time.time() or by retrieval from a database depending on implementation.
        """
        pass

    @abstractmethod
    def log(self, char_count: int, timestamp_secs: Optional[float]=None) -> None:
        """Log the character count usage.  Timestamp is set by caller such as by calling time.time()."""
        pass  # Implement the logging functionality here

    @abstractmethod
    def charcounts_and_timestamps(self, cutoff: Optional[float] = None,trunc_old:bool = True) -> list[(int, float)]:
        """
        Return a list of character counts and timestamps greater than the given cutoff.
        Items are returned in timestamp order from oldest to newest.

        If cutoff is None, uses self.cur_timestamp() - self.default_cutoff_secs 
        If trunc_old is True, may remove all entries older than the cutoff (but is not required to do so).
        """
        pass

class _InMemoryCharUsageLog(_CharUsageLogBase):
    """"Implements _CharUsageLogBase using an in-memory deque for character counts and timestamps."""
    def __init__(self):
        super().__init__()
        self.char_counts: deque[(int, float)] = deque()

    def cur_timestamp(self) -> float:
        return time.time()

    def log(self, char_count: int, timestamp_secs: Optional[float] = None) -> None:
        self.char_counts.append((char_count, timestamp_secs if timestamp_secs is not None else self.cur_timestamp()))   

    def charcounts_and_timestamps(self, cutoff: Optional[float] = None, trunc_old:bool = True) -> list[(int, float)]:
        if cutoff is None:
            cutoff = self.cur_timestamp() - self.default_cutoff_secs

        if trunc_old:
            while self.char_counts and self.char_counts[0][1] < cutoff:
                self.char_counts.popleft()

        return list(self.char_counts)


class _FilePersistedCharUsageLog(_CharUsageLogBase):
    """
    Implements _CharUsageLogBase using a file for character counts and timestamps.
    This class is intended to allow cross-process tracking of character usage counts.

    IMPLEMENTATION NOTE: Actually uses sqlite DB for thread safety
    """

    DB_DEFS = {"0000-00-01_table_defs.sql": """
        CREATE TABLE IF NOT EXISTS char_usage (
            char_count INTEGER NOT NULL,
            timestamp_secs REAL NOT NULL DEFAULT (strftime('%s', 'now'))
        ) ;
               
        CREATE INDEX IF NOT EXISTS idx_timestamp_secs ON char_usage (timestamp_secs);
               
        -- since_cutoff
        SELECT char_count, timestamp_secs 
        FROM char_usage 
        WHERE timestamp_secs > :cutoff 
        ORDER BY timestamp_secs ASC;

        -- since_cutoff_by_secs
        SELECT char_count, timestamp_secs
        FROM char_usage
        WHERE timestamp_secs > unixepoch('subsec') - :cutoff_secs;
               
        -- purge_older
        DELETE FROM char_usage 
        WHERE timestamp_secs <= :cutoff;
               
        -- purge_older_by_secs
        DELETE FROM char_usage
        WHERE timestamp_secs <= unixepoch('subsec') - :cutoff_secs;
               
        -- insert_row
        INSERT INTO char_usage (char_count, timestamp_secs)
        VALUES (:char_count, COALESCE(:timestamp_secs,unixepoch('subsec')));
               
    """}

    def __init__(self, sqlite_fname:str):
        super().__init__()
        self._dbjig:DbJig = DbJig(sqlite_fname,loadsources = self.__class__.DB_DEFS)
        

    def cur_timestamp(self) -> float:
        return float(self._dbjig.db().execute("SELECT unixepoch('subsec') AS timestamp").fetchone()[0])

    def log(self, char_count: int, timestamp_secs: Optional[float] = None) -> None:
        self._dbjig.pquery("insert_row", {'char_count': char_count, 'timestamp_secs': timestamp_secs})

    _PURGE_FREQ = 10 # only purge roughhly every nth call (arbitrary) to reduce database locking

    def charcounts_and_timestamps(self, cutoff: Optional[float] = None, trunc_old:bool = True) -> list[(int, float)]:
        should_purge = trunc_old and int(time.time()) % self.__class__._PURGE_FREQ == 0 
        retval = None
        if cutoff is None:
            if should_purge: # deliberately cautious about purchign
                self._dbjig.pquery("purge_older_by_secs", {'cutoff_secs': self.default_cutoff_secs*2})
            retval = self._dbjig.pquery("since_cutoff_by_secs", {'cutoff_secs': self.default_cutoff_secs})  
        else:
            if should_purge: # Deliberately cautious about purging here
                self._dbjig.pquery("purge_older", {'cutoff': cutoff-self.default_cutoff_secs})
            retval = self._dbjig.pquery("since_cutoff", {'cutoff': cutoff})  
        return [(int(row[0]),float(row[1])) for row in retval]
    

class SelfBandwidthThrottle:
    """Class to help self-throttle requests based on the number of characters sent in a given interval.
    
    Users should call suggest_throttle_seconds() before making a request with the char count of the planned call. 
    If the return value is None, the request can be made immediately.
    Otherwise, the return value is the number of seconds to wait before making the desired request.

    Args:
        char_limit: Maximum number of characters allowed in the interval
        interval_secs: The interval over which the counters reset. Default is 60 seconds.
        cross_process_logfile: Optional file path for cross-process throttling
        max_requests: Optional maximum number of requests allowed in the interval
    """
    def __init__(self, char_limit: int, interval_secs: int = 60, 
                 cross_process_logfile: Optional[str] = None,
                 max_requests: Optional[int] = None):
        self.char_limit = char_limit
        self.interval = interval_secs
        self.max_requests = max_requests
        self.char_count = 0
        self.last_reset_time = time.time()
        
        self._token_log: _CharUsageLogBase = (_InMemoryCharUsageLog() if cross_process_logfile is None 
                                             else _FilePersistedCharUsageLog(cross_process_logfile))
        self._token_log.default_cutoff_secs = interval_secs

    def _calc_soonest_send(self, desired_send_count: int) -> float:
        """Taking into account the character limit and the max requests limit, calculate the soonest time to safely send the desired number of characters considered traffic currently queued."""
        char_logs = self._token_log.charcounts_and_timestamps(trunc_old=True) or [(0,self._token_log.cur_timestamp())] # fake entry if empty
        soonest_send = char_logs[-1][1]  # timestamp of last entry or current time if no entries
        earliest_relevant_time = soonest_send - self.interval
        
        # Check character limit
        chars_in_window = 0
        for char_count, entry_timestamp in reversed(char_logs):
            if entry_timestamp < earliest_relevant_time:
                break
            chars_in_window += char_count
        
        if chars_in_window + desired_send_count > self.char_limit:
            # Need to wait for oldest entries to expire
            oldest_relevant_time = min(ts for _, ts in char_logs if ts >= earliest_relevant_time)
            soonest_send = max(soonest_send, oldest_relevant_time + self.interval)

        # Check max requests limit
        if self.max_requests is not None:
            request_count = len([ct for ct,ts in char_logs if ts >= earliest_relevant_time])
            if request_count >= self.max_requests:
                # Find oldest request in window to calculate when we can make another request
                oldest_request_time = min(ts for _,ts in char_logs if ts >= earliest_relevant_time)
                soonest_send = max(soonest_send, oldest_request_time + self.interval)

        return soonest_send

    def suggest_throttle_seconds(self, request_chars: int, test_only:bool = False) -> float:
        """
        Throttles the request if the character count or request count exceeds the limit within the specified interval.
        Returns the time to wait (in seconds) before making the request, or zero if the request can be made immediately.
        This method presumes that the caller will wait the suggested time before making the request.

        Note, regardless of whether it indicates non-zero wait, it will log a future entry under the supposition 
        that the request will be waited for and sent later. This behavior is particularly important so that in 
        multi-sender scenarios, the different senders can "reserve" their future token use.

        Args:
            request_chars: Number of characters in the planned request
            test_only: If True, the method will not log the request, but will still calculate the wait time.
        """
        soonest_send = self._calc_soonest_send(request_chars)
        current_time = self._token_log.cur_timestamp()
        wait_time = max(0, soonest_send - current_time)
        
        if not test_only and wait_time == 0:  # Only log if not testing and no wait needed
            self._token_log.log(request_chars, current_time)  # Log at current time, not future time
        
        return wait_time

    def sent_window_char_count(self) -> int:
        """Returns the total number of characters sent in the current interval, removing expired entries."""
        # below line automatically triggers any necessary purging of old entries
        return sum(ct for ct,_ in self._token_log.charcounts_and_timestamps())


    async def sleep_on_throttle(self, request_chars: int):
        """Sleeps if the throttle is encountered to expire before returning.
           Use suggest_throttle_seconds() if sleeping is not what you want to do.

           BE AWARE: This method is deliberately async to allow for other async operations to continue while waiting.
        """
        throttle_seconds = self.suggest_throttle_seconds(request_chars)
        if throttle_seconds is not None and throttle_seconds > 0:
            logger.info(f"Self-throttling sleep() triggered for {throttle_seconds:.1f} seconds in order to fit {request_chars:_} into interval limit of {self.char_limit:_} chars.")
            await asyncio.sleep(throttle_seconds)



    def _calc_soonest_send(self, desired_send_count: int) -> float:
        """Taking into account the character limit and the max requests limit, calculate the soonest time to safely send the desired number of characters considered traffic currently queued."""
        char_logs = self._token_log.charcounts_and_timestamps(trunc_old=True) or [(0,self._token_log.cur_timestamp())] # fake entry if empty
        soonest_send = char_logs[-1][1]  # timestamp of last entry or current time if no entries
        earliest_relevant_time = soonest_send - self.interval
        
        # Check character limit
        chars_in_window = 0
        for char_count, entry_timestamp in reversed(char_logs):
            if entry_timestamp < earliest_relevant_time:
                break
            chars_in_window += char_count
        
        if chars_in_window + desired_send_count > self.char_limit:
            # Need to wait for oldest entries to expire
            oldest_relevant_time = min(ts for _, ts in char_logs if ts >= earliest_relevant_time)
            soonest_send = max(soonest_send, oldest_relevant_time + self.interval + 0.01)

        # Check max requests limit
        if self.max_requests is not None:
            request_count = len([ct for ct,ts in char_logs if ts >= earliest_relevant_time])
            if request_count >= self.max_requests:
                oldest_request_time = min(ts for _,ts in char_logs if ts >= earliest_relevant_time)
                soonest_send = max(soonest_send, oldest_request_time + self.interval + 0.01)

        return soonest_send

    def suggest_throttle_seconds(self, request_chars: int, test_only:bool = False) -> float:
        """
        Throttles the request if the character count exceeds the limit within the specified interval.
        Returns the time to wait (in seconds) before making the request, or zero if the request can be made immediately.
        This method presumes that the caller will wait the suggested time before making the request.

        Note, regardless of whether it indicates non-zero wait, it will log a future entry under the supposition that the request will be waited for and sent later.
        This behavior is particularlly important so that in multi-sender scenarios, the different senders can "reserve" their future token use.

        test_only: If True, the method will not log the request, but will still calculate the wait time.
        """

        soonest_send = self._calc_soonest_send(request_chars)
        current_time = self._token_log.cur_timestamp()
        if not test_only:
            self._token_log.log(request_chars,soonest_send)  # log presumed send
        return max(0, soonest_send - current_time) 
