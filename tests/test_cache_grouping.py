# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for CacheGrouping facet pattern.

These tests verify that the CacheGrouping class provides a clean API
for working with specific groupings within a CachedFileFolders cache.
"""

import pytest
import tempfile
from pathlib import Path
from totodev_pub.cached_file_folders import CachedFileFolders
from totodev_pub.cached_file_folders_support.cache_grouping import CacheGrouping
from totodev_pub.cached_file_folders_support.file_proxy_data_struct import SerializableDataProxy


@pytest.fixture
def cache():
    """Create a temporary cache for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_root = Path(tmpdir) / "test_cache"
        cache = CachedFileFolders(
            grouping_pattern="projects/{project}/docs/",
            root_dir=str(cache_root)
        )
        yield cache
        cache.close()


@pytest.mark.asyncio
async def test_grouping_factory_method(cache):
    """Test that grouping() factory method creates valid CacheGrouping."""
    grouping = cache.grouping(["webapp"])
    
    assert isinstance(grouping, CacheGrouping)
    assert grouping.grouping_key == ["webapp"]
    assert grouping.cache_root_dir == cache.root_dir
    assert grouping.pattern == cache.pattern
    assert grouping.parent_cache is cache


@pytest.mark.asyncio
async def test_grouping_upsert_and_files(cache):
    """Test upsert_file and files methods on CacheGrouping."""
    grouping = cache.grouping(["webapp"])
    
    # Upsert a file using the facet
    doc = SerializableDataProxy(
        {"title": "Test Doc", "content": "Test content"},
        "test.json"
    )
    notice = await grouping.upsert_file(doc)
    
    assert notice is not None
    assert notice.ref_path == "test.json"
    
    # Verify file appears in files()
    files = list(grouping.files())
    assert len(files) == 1
    assert files[0].ref_path == "test.json"
    
    # Verify files_count
    assert grouping.files_count() == 1


@pytest.mark.asyncio
async def test_grouping_find_and_exists(cache):
    """Test find_file and file_exists methods on CacheGrouping."""
    grouping = cache.grouping(["webapp"])
    
    # Add a file
    doc = SerializableDataProxy({"data": "test"}, "test.json")
    await grouping.upsert_file(doc)
    
    # Test find_file
    found = grouping.find_file("test.json")
    assert found is not None
    assert found.ref_path == "test.json"
    
    # Test file_exists
    assert grouping.file_exists("test.json")
    assert not grouping.file_exists("nonexistent.json")


@pytest.mark.asyncio
async def test_grouping_delete(cache):
    """Test delete_file method on CacheGrouping."""
    grouping = cache.grouping(["webapp"])
    
    # Add and then delete a file
    doc = SerializableDataProxy({"data": "test"}, "test.json")
    await grouping.upsert_file(doc)
    
    assert grouping.file_exists("test.json")
    
    notice = await grouping.delete_file("test.json")
    assert notice is not None
    assert not grouping.file_exists("test.json")


@pytest.mark.asyncio
async def test_groupings_iterator(cache):
    """Test that groupings() returns CacheGrouping objects."""
    # Add files to multiple groupings
    webapp = cache.grouping(["webapp"])
    api = cache.grouping(["api"])
    
    await webapp.upsert_file(SerializableDataProxy({"a": 1}, "w1.json"))
    await api.upsert_file(SerializableDataProxy({"b": 2}, "a1.json"))
    
    # Iterate groupings
    groupings = list(cache.groupings())
    
    assert len(groupings) == 2
    assert all(isinstance(g, CacheGrouping) for g in groupings)
    
    # Verify grouping keys
    keys = {tuple(g.grouping_key) for g in groupings}
    assert keys == {("webapp",), ("api",)}


def test_grouping_equality(cache):
    """Test equality comparison of CacheGrouping objects."""
    webapp1 = cache.grouping(["webapp"])
    webapp2 = cache.grouping(["webapp"])
    api = cache.grouping(["api"])
    
    # Same grouping key should be equal
    assert webapp1 == webapp2
    
    # Different grouping keys should not be equal
    assert webapp1 != api
    assert webapp2 != api


def test_grouping_ordering(cache):
    """Test ordering comparison of CacheGrouping objects."""
    webapp = cache.grouping(["webapp"])
    api = cache.grouping(["api"])
    mobile = cache.grouping(["mobile"])
    
    # Lexicographic ordering by grouping_key
    assert api < mobile < webapp
    assert webapp > mobile > api
    assert api <= mobile <= webapp
    assert webapp >= mobile >= api


def test_grouping_hashing(cache):
    """Test that CacheGrouping objects are hashable and can be used in sets/dicts."""
    webapp1 = cache.grouping(["webapp"])
    webapp2 = cache.grouping(["webapp"])
    api = cache.grouping(["api"])
    
    # Should be usable in sets
    grouping_set = {webapp1, webapp2, api}
    assert len(grouping_set) == 2  # webapp1 and webapp2 are equal
    
    # Should be usable as dict keys
    grouping_dict = {webapp1: "webapp", api: "api"}
    assert grouping_dict[webapp2] == "webapp"  # webapp2 hashes same as webapp1


def test_grouping_ordering_different_caches():
    """Test that comparing groupings from different caches raises ValueError."""
    with tempfile.TemporaryDirectory() as tmpdir1:
        with tempfile.TemporaryDirectory() as tmpdir2:
            cache1 = CachedFileFolders("projects/{project}/", str(Path(tmpdir1) / "cache1"))
            cache2 = CachedFileFolders("projects/{project}/", str(Path(tmpdir2) / "cache2"))
            
            try:
                g1 = cache1.grouping(["webapp"])
                g2 = cache2.grouping(["webapp"])
                
                # Equality should work (returns False)
                assert g1 != g2
                
                # But ordering should raise ValueError
                with pytest.raises(ValueError, match="Cannot compare groupings from different caches"):
                    g1 < g2
                
                with pytest.raises(ValueError, match="Cannot compare groupings from different caches"):
                    g1 > g2
            finally:
                cache1.close()
                cache2.close()


def test_grouping_string_representations(cache):
    """Test __str__ and __repr__ methods."""
    grouping = cache.grouping(["webapp"])
    
    # Test __str__
    str_repr = str(grouping)
    assert "webapp" in str_repr
    
    # Test __repr__
    repr_str = repr(grouping)
    assert "CacheGrouping" in repr_str
    assert "grouping_key" in repr_str


def test_grouping_properties(cache):
    """Test that CacheGrouping properties work correctly."""
    grouping = cache.grouping(["webapp"])
    
    assert grouping.grouping_key == ["webapp"]
    assert grouping.parent_cache is cache
    with pytest.raises(
        AttributeError,
        match="CacheGrouping.root_dir was removed; use CacheGrouping.cache_root_dir instead",
    ):
        _ = grouping.root_dir
    assert grouping.pattern == cache.pattern
    
    # folder_path should be a valid Path
    assert isinstance(grouping.folder_path, Path)
    assert "webapp" in str(grouping.folder_path)


@pytest.mark.asyncio
async def test_grouping_resync_bulk(cache):
    """Test resync_bulk method on CacheGrouping."""
    grouping = cache.grouping(["webapp"])
    
    # Create test proxies
    proxies = [
        SerializableDataProxy({"id": 1}, "file1.json"),
        SerializableDataProxy({"id": 2}, "file2.json"),
        SerializableDataProxy({"id": 3}, "file3.json"),
    ]
    
    # Resync using the facet
    result = await grouping.resync_bulk(proxies)
    
    assert len(result.changes) == 3
    assert len(result.failures) == 0
    assert grouping.files_count() == 3


def test_groupings_can_be_sorted(cache):
    """Test that groupings can be sorted."""
    # Create groupings in non-alphabetical order
    groupings = [
        cache.grouping(["zebra"]),
        cache.grouping(["apple"]),
        cache.grouping(["mango"]),
    ]
    
    # Sort them
    sorted_groupings = sorted(groupings)
    
    # Verify ordering
    keys = [g.grouping_key for g in sorted_groupings]
    assert keys == [["apple"], ["mango"], ["zebra"]]


def test_grouping_flat_pattern():
    """Test that grouping works with flat patterns (no grouping keys)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_root = Path(tmpdir) / "flat_cache"
        cache = CachedFileFolders(
            grouping_pattern="cache/",
            root_dir=str(cache_root)
        )
        
        try:
            # For flat patterns, grouping_key is normalized to empty tuple
            grouping = cache.grouping(None)
            
            assert grouping.grouping_key == ()
            assert grouping.parent_cache is cache
            
            # Should raise ValueError if trying to use groupings() on flat pattern
            with pytest.raises(ValueError, match="not applicable for flat patterns"):
                list(cache.groupings())
        finally:
            cache.close()

