# Part of the totodev_pub library.

from totodev_pub.case_manager_support.shutdown import (
    ShutdownRequest,
    discard_stale_requests,
    scan_shutdown_intake,
    write_shutdown_request,
)


def test_empty_intake_yields_none(tmp_path):
    assert scan_shutdown_intake(tmp_path / "missing") is None
    (tmp_path / "intake").mkdir()
    assert scan_shutdown_intake(tmp_path / "intake") is None


def test_hand_touched_file_means_immediate(tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "please_stop").touch()
    directive = scan_shutdown_intake(intake)
    assert directive is not None
    assert directive.graceful is False
    assert directive.correlation_id is None


def test_sigterm_filename_token_means_graceful(tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "drain_SIGTERM_now.txt").touch()
    assert scan_shutdown_intake(intake).graceful is True
    (intake / "drain_SIGTERM_now.txt").unlink()
    (intake / "sigterm_lowercase").touch()  # case-insensitive
    assert scan_shutdown_intake(intake).graceful is True


def test_parsed_content_is_authoritative_over_filename(tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    req = ShutdownRequest(correlation_id="c1", requested_at="2026-07-09T00:00:00Z", graceful=False)
    req.save(str(intake / "SIGTERM_but_content_says_immediate.yaml"), retain_lock=False)
    directive = scan_shutdown_intake(intake)
    assert directive.graceful is False        # content wins
    assert directive.correlation_id == "c1"


def test_dotfiles_are_invisible(tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / ".half-written.yaml").touch()
    assert scan_shutdown_intake(intake) is None


def test_write_shutdown_request_atomic_shape(tmp_path):
    intake = tmp_path / "intake"
    corr, path = write_shutdown_request(intake, graceful=True, reason="drain please")
    assert path.parent == intake
    assert not path.name.startswith(".")
    assert "SIGTERM" in path.name             # discoverable even without parsing
    directive = scan_shutdown_intake(intake)
    assert directive.graceful is True
    assert directive.reason == "drain please"
    assert directive.correlation_id == corr


def test_discard_stale_requests(tmp_path):
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "stale_one").touch()
    (intake / "stale_two.yaml").touch()
    (intake / ".tmp-ignored").touch()
    assert discard_stale_requests(intake) == 2
    assert scan_shutdown_intake(intake) is None
