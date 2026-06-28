from pathlib import Path

from totodev_pub.folder_backed_case_support.case_assets import CaseAssets


def _write_text(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_add_keep_rules_accepts_absolute_exact_path(tmp_path):
    assets = CaseAssets(tmp_path / "case-abs-exact")
    _write_text(assets.asset_path("keep/me.txt"))
    _write_text(assets.asset_path("drop/me.txt"))

    absolute_keep = assets.asset_path("keep/me.txt")
    assets.add_keep_rules(str(absolute_keep))

    assert assets.keep_list() == ["keep/me.txt"]
    purged = assets.purge_ephemeral()

    assert purged == ["drop/me.txt"]
    assert assets.asset_path("keep/me.txt").exists()
    assert not assets.asset_path("drop/me.txt").exists()


def test_add_keep_rules_accepts_absolute_glob_path(tmp_path):
    assets = CaseAssets(tmp_path / "case-abs-glob")
    _write_text(assets.asset_path("results/a.json"))
    _write_text(assets.asset_path("results/b.txt"))
    _write_text(assets.asset_path("other/c.json"))

    absolute_glob = f"{assets.folder.as_posix()}/results/*.json"
    assets.add_keep_rules(absolute_glob)

    assert assets.keep_list() == ["results/*.json"]
    purged = assets.purge_ephemeral()

    assert purged == ["other/c.json", "results/b.txt"]
    assert assets.asset_path("results/a.json").exists()
    assert not assets.asset_path("results/b.txt").exists()
    assert not assets.asset_path("other/c.json").exists()


def test_remove_keep_rules_normalizes_absolute_rules(tmp_path):
    assets = CaseAssets(tmp_path / "case-remove-abs")
    assets.add_keep_rules("reports/*.csv")
    absolute_glob = f"{assets.folder.as_posix()}/reports/*.csv"

    assets.remove_keep_rules(absolute_glob)

    assert assets.keep_list() == []
