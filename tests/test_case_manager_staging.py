# Part of the totodev_pub library.

from case_manager_test_utils import provision_manager


def test_allocate_staging_folder(tmp_path):
    manager = provision_manager(tmp_path)
    path = manager.allocate_staging_folder()
    assert path.is_dir()
    assert path.parent.name == "staging"
