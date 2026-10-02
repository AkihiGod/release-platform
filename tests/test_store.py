"""发布历史仓储的测试。每条都用临时库，别碰真的 releases.db。"""
import pytest

import store


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    return store


def test_record_then_history_newest_first(tmp_db):
    tmp_db.record("app", "v1", "sha1", "success", "me", "t0", "t1", "首发")
    tmp_db.record("app", "v2", "sha2", "failed", "me", "t2", "t3", "")

    rows = tmp_db.history("app")
    assert [r["version"] for r in rows] == ["v2", "v1"]
    assert rows[0]["status"] == "failed"
    assert rows[1]["note"] == "首发"


def test_history_respects_limit(tmp_db):
    for i in range(5):
        tmp_db.record("app", f"v{i}", "s", "success", "me", "", "")
    assert len(tmp_db.history("app", limit=3)) == 3


def test_history_filters_by_app(tmp_db):
    tmp_db.record("app", "v1", "s", "success", "me", "", "")
    tmp_db.record("other", "vx", "s", "success", "me", "", "")

    assert [r["version"] for r in tmp_db.history("app")] == ["v1"]
    assert [r["version"] for r in tmp_db.history("other")] == ["vx"]


def test_status_text_covers_every_status_used():
    # 落库用的是英文码，显示前要翻成中文；这里防的是加了新状态忘了加翻译
    used_statuses = {"success", "failed", "rolled_back", "rollback"}
    assert used_statuses <= set(store.STATUS_TEXT)
