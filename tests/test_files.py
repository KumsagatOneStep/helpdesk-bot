from datetime import UTC, datetime

from helpdesk.files import new_file_path


def test_new_file_path_layout(tmp_path):
    p = new_file_path(str(tmp_path), "Отчёт.PDF", datetime(2026, 10, 7, tzinfo=UTC))
    assert p.parent == tmp_path / "2026" / "10"
    assert p.parent.is_dir()
    assert p.suffix == ".pdf"
    assert not p.exists()


def test_new_file_path_without_or_weird_name(tmp_path):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    assert new_file_path(str(tmp_path), None, now).suffix == ""
    assert new_file_path(str(tmp_path), "x.verylongextension", now).suffix == ""
    a = new_file_path(str(tmp_path), "a.jpg", now)
    b = new_file_path(str(tmp_path), "a.jpg", now)
    assert a != b
