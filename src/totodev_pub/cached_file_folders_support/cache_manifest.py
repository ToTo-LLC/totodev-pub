# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Cache manifest model for `CachedFileFolders` persisted as JSON.

This file defines a Pydantic v2 model (using `FileMappedPydanticMixin`) that
is stored at `<root_dir>/.cached_file_folders.json` to record constructor
parameters required to safely re-attach to an existing cache.

Notes:
- Manifest is machine-generated/maintained; humans may read it for context.
- We persist the effective character replacement map used by the cache.
"""

from __future__ import annotations

from typing import Dict, Optional
from datetime import datetime
from pydantic import BaseModel, Field
from pathlib import Path

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin


MANIFEST_FILENAME = ".cached_file_folders.json"


class CachedFileFoldersManifest(BaseModel, FileMappedPydanticMixin):
    purpose: str = Field(
        default=(
            "Machine-generated cache manifest for CachedFileFolders. "
            "Records effective creation parameters for safe re-attachment. "
            "This file is generated and maintained by the system."
        )
    )
    schema_version: int = 1
    class_name: str = "CachedFileFolders"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    package_version: Optional[str] = None

    root_dir: str
    grouping_pattern: str
    use_xxhash: bool
    slave_dir_extension: str
    char_replacement_map: Dict[str, str]

    @staticmethod
    def manifest_path_for_root(root_dir: str | Path) -> Path:
        root = Path(root_dir)
        return root / MANIFEST_FILENAME

    def validate_against_parameters(
        self,
        root_dir: str,
        grouping_pattern: str,
        use_xxhash: bool,
        slave_dir_extension: str,
        char_replacement_map: Dict[str, str],
    ) -> Dict[str, tuple]:
        """
        Validate manifest against provided parameters.
        
        Returns:
            Dictionary of mismatches: {field_name: (manifest_value, provided_value)}
            Empty dict if all parameters match.
        """
        mismatches = {}
        if self.root_dir != root_dir:
            mismatches['root_dir'] = (self.root_dir, root_dir)
        if self.grouping_pattern != grouping_pattern:
            mismatches['grouping_pattern'] = (self.grouping_pattern, grouping_pattern)
        if self.use_xxhash != use_xxhash:
            mismatches['use_xxhash'] = (self.use_xxhash, use_xxhash)
        if self.slave_dir_extension != slave_dir_extension:
            mismatches['slave_dir_extension'] = (self.slave_dir_extension, slave_dir_extension)
        if self.char_replacement_map != char_replacement_map:
            mismatches['char_replacement_map'] = (self.char_replacement_map, char_replacement_map)
        return mismatches

    @classmethod
    def load_and_validate(
        cls,
        root_dir: str,
        grouping_pattern: str,
        use_xxhash: bool,
        slave_dir_extension: str,
        char_replacement_map: Dict[str, str],
    ) -> tuple[Optional['CachedFileFoldersManifest'], bool]:
        """
        Load existing manifest and validate against parameters.
        
        Returns:
            Tuple of (manifest, loaded_successfully)
            - If manifest loads and validates: (manifest, True)
            - If manifest missing or corrupt: (None, False)
        
        Raises:
            ValueError: If manifest exists, loads, but validation fails
        """
        import logging
        logger = logging.getLogger(__name__)
        
        manifest_path = cls.manifest_path_for_root(root_dir)
        
        if not manifest_path.exists():
            return None, False
        
        try:
            manifest = cls.load(str(manifest_path), acquire_lock=False, format_override="json")
            mismatches = manifest.validate_against_parameters(
                root_dir=root_dir,
                grouping_pattern=grouping_pattern,
                use_xxhash=use_xxhash,
                slave_dir_extension=slave_dir_extension,
                char_replacement_map=char_replacement_map,
            )
            
            if mismatches:
                details = "; ".join([f"{k}: existing={v[0]!r} new={v[1]!r}" for k, v in mismatches.items()])
                raise ValueError(
                    "Cache manifest parameter mismatch. "
                    f"{details}. Delete the manifest at '{manifest_path}' or use a different root."
                )
            
            return manifest, True
            
        except ValueError:
            # Re-raise validation errors
            raise
        except Exception as e:
            logger.warning("Manifest at %s unreadable or invalid: %s", manifest_path, e)
            return None, False

    @classmethod
    def create_new(
        cls,
        root_dir: str,
        grouping_pattern: str,
        use_xxhash: bool,
        slave_dir_extension: str,
        char_replacement_map: Dict[str, str],
        warn_if_data_exists: bool = True,
    ) -> 'CachedFileFoldersManifest':
        """
        Create and save a new manifest file.
        
        Args:
            warn_if_data_exists: If True, log warning if root directory has existing data
        
        Returns:
            The newly created manifest
        """
        import logging
        logger = logging.getLogger(__name__)
        
        manifest_path = cls.manifest_path_for_root(root_dir)
        
        # Check for existing data and warn if requested
        if warn_if_data_exists:
            root_path = Path(root_dir)
            if root_path.exists():
                has_existing_data = any(
                    p != manifest_path for p in root_path.iterdir()
                ) if root_path.exists() else False
                if has_existing_data:
                    logger.warning(
                        "No manifest found at %s, but data exists under root. "
                        "Adopting existing data and writing manifest.",
                        manifest_path,
                    )
        
        # Create manifest
        manifest = cls(
            root_dir=root_dir,
            grouping_pattern=grouping_pattern,
            use_xxhash=use_xxhash,
            slave_dir_extension=slave_dir_extension,
            char_replacement_map=char_replacement_map,
        )
        
        # Save to file
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest.save(file_path=str(manifest_path), format_override="json")
        
        return manifest


