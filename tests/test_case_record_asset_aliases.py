import pytest
from pydantic import ValidationError

from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.helpers import _utcnow


def _kwargs(**extra):
    base = dict(case_object_type="X", case_id="c1", created=_utcnow())
    base.update(extra)
    return base


def test_asset_aliases_is_required():
    with pytest.raises(ValidationError):
        CaseRecord(**_kwargs())


def test_asset_aliases_round_trips(tmp_path):
    path = tmp_path / "case_record.yaml"
    entry = {"rlist": {"path": "receipts/rlist.json", "deserializer": "ReceiptListRecord"}}
    rec = CaseRecord(**_kwargs(asset_aliases=entry))
    rec.save(str(path))
    reloaded = CaseRecord.open(str(path), without_lock=True)
    assert reloaded.asset_aliases == entry
