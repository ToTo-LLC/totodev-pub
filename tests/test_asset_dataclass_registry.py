import pytest
from pydantic import BaseModel

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin
from totodev_pub.folder_backed_case_support.asset_dataclass_registry import (
    AssetDataclassRegistry,
)


class _Rec(BaseModel, FileMappedPydanticMixin):
    n: int = 0


def test_register_and_resolve():
    reg = AssetDataclassRegistry()
    reg.register(_Rec)
    assert reg.resolve("_Rec") is _Rec
    assert reg.resolve("Unknown") is None
    assert reg.resolve(None) is None


def test_register_rejects_non_filemapped():
    reg = AssetDataclassRegistry()

    class Plain:
        pass

    with pytest.raises(ValueError):
        reg.register(Plain)
