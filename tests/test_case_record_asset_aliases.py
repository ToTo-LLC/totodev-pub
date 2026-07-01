import pytest
from pydantic import BaseModel, ValidationError

from totodev_pub.folder_backed_case_support.case_record import CaseRecord
from totodev_pub.folder_backed_case_support.helpers import _utcnow


def _kwargs(**extra):
    base = dict(
        case_object_type="X",
        case_id="c1",
        created=_utcnow(),
        fsm_state_chains=[],
    )
    base.update(extra)
    return base


def test_asset_aliases_is_required():
    with pytest.raises(ValidationError):
        CaseRecord(**_kwargs())


def test_asset_aliases_round_trips(tmp_path):
    path = tmp_path / "case_record.yaml"
    entry = {"rlist": {"path": "receipts/rlist.json", "loader": "ReceiptListRecord"}}
    rec = CaseRecord(**_kwargs(asset_aliases=entry))
    rec.save(str(path))
    reloaded = CaseRecord.open(str(path), without_lock=True)
    assert reloaded.asset_aliases == entry


def test_asset_aliases_states_round_trip(tmp_path):
    path = tmp_path / "case_record.yaml"
    entry = {
        "ticket": {
            "path": "ticket.yaml",
            "loader": "TicketForm",
            "states": ["closed", "new", "open"],
        },
    }
    rec = CaseRecord(**_kwargs(asset_aliases=entry))
    rec.save(str(path))
    reloaded = CaseRecord.open(str(path), without_lock=True)
    assert reloaded.asset_aliases == entry


def test_asset_aliases_rejects_unknown_key():
    with pytest.raises(ValidationError, match="unknown key"):
        CaseRecord(
            **_kwargs(
                asset_aliases={"x": {"path": "x.json", "deserializer": "OldName"}},
            )
        )


def test_asset_aliases_rejects_bad_path_type():
    with pytest.raises(ValidationError, match="path"):
        CaseRecord(**_kwargs(asset_aliases={"x": {"path": 123}}))


def test_asset_aliases_rejects_bad_loader_type():
    with pytest.raises(ValidationError, match="loader"):
        CaseRecord(**_kwargs(asset_aliases={"x": {"path": "x.json", "loader": 1}}))


def test_asset_aliases_rejects_bad_states_type():
    with pytest.raises(ValidationError, match="states"):
        CaseRecord(
            **_kwargs(asset_aliases={"x": {"path": "x.json", "states": "open"}}),
        )
