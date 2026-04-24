# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub


from typing import Any, Callable, Sequence,Union
import time
from datetime import datetime, timedelta

class ForgetfulReader:
    """Utility class for putting a cache in from of a high-cost data retrieval
    operation.  This class requires that the data being access be keyed by
    an array or tuple.
    """
    def __init__(self, value_retriever: Union[None,Callable[[Sequence[Any], Any], Any]], expiration_seconds: int) -> None:
        """
        The value_retriever callable takes a key (as Tuple/List) and returns the corresponding
        value.  If the value isn't found in the cache, the value_retriever is called to retrieve.

        Any fallback logic (or exceptions) needed should be in the value_retriever.
        Note that if expiration_seconds is 0, the cache is never used and this class is essentially a passthrough.
        """
        self._value_retriever = value_retriever
        self.expiration_seconds = expiration_seconds
        self._cache = {}

    def _is_expired(self, key: Sequence[Any]) -> bool:
        """Indicates that the next request for this key should be retrieved from the source."""
        return key not in self._cache or datetime.now() > self._cache[key]['expiration']

    def expire(self,key: Sequence[Any]) -> None:
        """Force the cache to expire for a given key"""
        if self.expiration_seconds == 0:
            return  #no-op if we're not caching
        del self._cache[key]

    def override(self, key: Sequence[Any], value: Any) -> None:
        """Override the value in the cache for a given key"""
        if self.expiration_seconds == 0:
            return  #no-op if we're not caching
        self._cache[key] = {
            'value': value,
            'expiration': datetime.now() + timedelta(seconds=self.expiration_seconds)
        }

    COMPACT_CHECK_PERIODICITY = 23  # roughly how often to check for expired entries

    def retrieve(self, key: Sequence[Any]) -> Any:
        if self.expiration_seconds == 0:  #never cachee
            return self._value_retriever(key) 
        if time.time() % self.COMPACT_CHECK_PERIODICITY == 0:
            self.compact()  # semi-randomly perform a compact
        value = self._value_retriever(key)
        self._cache[key] = {
            'value': value,
            'expiration': datetime.now() + timedelta(seconds=self.expiration_seconds)
        }
        return value

    def get(self, key: Sequence[Any]) -> Any:
        if self._is_expired(key):
            return self.retrieve(key)
        #TODO: not to nitpick but this AI generated code has two lookups when it probably should have 1
        return self._cache[key]['value']

    def compact(self) -> None:
        """Remove expired entries from the cache"""
        if self.expiration_seconds == 0:
            return
        now = datetime.now()
        for key in list(self._cache.keys()):
            if now > self._cache[key]['expiration']:
                del self._cache[key]

    def flush(self) -> None:
        """Clear the cache"""
        self._cache.clear()