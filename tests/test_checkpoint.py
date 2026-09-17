"""``CheckpointStore`` 测试：增量写入、主键去重、旧 JSON 迁移、损坏容错。

断点是「中断后可续传」的唯一依据，写坏一次就会让用户重扫几小时，
因此这里的容错分支（损坏文件、非法 _key、二次迁移）都要有覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from checkpoint import CheckpointStore

Rec = Callable[..., dict[str, Any]]


@pytest.fixture
def cpdir(tmp_path: Path) -> Path:
    d = tmp_path / "cp"
    d.mkdir()
    return d


# --------------------------------------------------------------------------- #
# 路径规范化
# --------------------------------------------------------------------------- #


def test_json_path_is_rewritten_to_db(cpdir: Path) -> None:
    st = CheckpointStore(str(cpdir / "checkpoint_1_2.json"))
    assert st.path.endswith(".db")
    assert not st.path.endswith(".json")


def test_legacy_json_path_is_remembered(cpdir: Path) -> None:
    st = CheckpointStore(str(cpdir / "checkpoint_1_2.json"))
    assert st.legacy_json.endswith(".json")
    assert st.legacy_json == str(cpdir / "checkpoint_1_2.json")


def test_db_path_derives_legacy_json(cpdir: Path) -> None:
    st = CheckpointStore(str(cpdir / "pair.db"))
    assert st.path == str(cpdir / "pair.db")
    assert st.legacy_json == str(cpdir / "pair.json")


# --------------------------------------------------------------------------- #
# 基本存取
# --------------------------------------------------------------------------- #


def test_first_save_and_reload(cpdir: Path, rec: Rec) -> None:
    st = CheckpointStore(str(cpdir / "cp.db"))
    r1 = [rec("转发", "A→B", "甲", "w1", "a1")]
    r1[0]["_key"] = ["k1", "转发", "A→B", "甲"]
    st.save(r1, {"m1", "m2"}, 1000.0, {"A微博数": 2}, True, [{"mid": "m9", "err": "boom"}])

    cp = st.load()
    assert len(cp["records"]) == 1
    assert set(cp["scanned_mids"]) == {"m1", "m2"}
    assert cp["last_run_ts"] == 1000.0
    assert cp["stats"]["A微博数"] == 2
    assert cp["like_api_ok"] is True
    assert len(cp["failed_weibos"]) == 1
    assert cp["failed_weibos"][0]["mid"] == "m9"


def test_load_on_missing_file_returns_empty_structure(cpdir: Path) -> None:
    cp = CheckpointStore(str(cpdir / "never-written.db")).load()
    assert cp["records"] == []
    assert cp["scanned_mids"] == []
    assert cp["last_run_ts"] == 0.0
    assert cp["like_api_ok"] is None
    assert cp["failed_weibos"] == []


def test_like_api_ok_false_round_trip(cpdir: Path) -> None:
    st = CheckpointStore(str(cpdir / "cp.db"))
    st.save([], set(), 1.0, {}, False, [])
    assert st.load()["like_api_ok"] is False


# --------------------------------------------------------------------------- #
# 增量语义
# --------------------------------------------------------------------------- #


def test_incremental_append_only_writes_new_records(cpdir: Path, rec: Rec) -> None:
    st = CheckpointStore(str(cpdir / "cp.db"))
    r1 = [rec("转发", "A→B", "甲", "w1", "a1")]
    r1[0]["_key"] = ["k1", "转发", "A→B", "甲"]
    st.save(r1, {"m1", "m2"}, 1000.0, {"A微博数": 2}, True, [{"mid": "m9", "err": "boom"}])

    r2 = [*r1, rec("点赞", "A→B", "甲", "w2", "a2")]
    r2[1]["_key"] = ["k2", "点赞", "A→B", "甲"]
    st.save(
        r2,
        {"m1", "m2", "m3"},
        2000.0,
        {"A微博数": 3},
        True,
        [{"mid": "m9", "err": "boom"}, {"mid": "m10", "err": "boom2"}],
    )

    cp = st.load()
    assert len(cp["records"]) == 2, "重复写入会破坏「只写新增」的语义"
    assert len(cp["scanned_mids"]) == 3
    assert cp["last_run_ts"] == 2000.0
    assert len(cp["failed_weibos"]) == 2, "失败项应按 mid 去重"


def test_saving_same_batch_twice_does_not_duplicate(cpdir: Path, rec: Rec) -> None:
    st = CheckpointStore(str(cpdir / "cp.db"))
    r = [{**rec("转发", "A→B", "甲", "w1", "a1"), "_key": ["k1"]}]
    st.save(r, {"m1"}, 1.0, {}, True, [])
    st.load()  # 建立「已落库」基线
    st.save(r, {"m1"}, 1.0, {}, True, [])
    assert len(st.load()["records"]) == 1


def test_record_without_key_is_never_deduplicated(cpdir: Path) -> None:
    """``_key`` 为 None 表示该类型不做去重（与原 JSON 实现保持一致）。"""
    st = CheckpointStore(str(cpdir / "nokey.db"))
    nokey: dict[str, Any] = {"时间": "t", "互动类型": "点赞", "方向": "A→B", "发起方": "甲", "_key": None}
    st.save([dict(nokey), dict(nokey)], set(), 1.0, {}, True, [])
    assert len(st.load()["records"]) == 2


def test_fifty_then_fifty_one(cpdir: Path, rec: Rec) -> None:
    st = CheckpointStore(str(cpdir / "incr.db"))
    big = [{**rec("转发", "A→B", "甲", f"w{i}", "a"), "_key": ["k", i]} for i in range(50)]
    st.save(big, set(), 1.0, {}, True, [])
    st.save([*big, {**rec("转发", "A→B", "甲", "w50", "a"), "_key": ["k", 50]}], set(), 2.0, {}, True, [])
    assert len(st.load()["records"]) == 51


# --------------------------------------------------------------------------- #
# 旧 JSON 迁移
# --------------------------------------------------------------------------- #


def _write_legacy(path: Path, rec: Rec) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "records": [rec("评论", "B→A", "乙", "old", "c")],
                "scanned_mids": ["old1", "old2"],
                "last_run_ts": 555.0,
                "stats": {"B微博数": 7},
                "like_api_ok": False,
                "failed_weibos": [{"mid": "of1", "err": "x"}],
            },
            f,
            ensure_ascii=False,
        )


def test_legacy_json_is_migrated(cpdir: Path, rec: Rec) -> None:
    old = cpdir / "legacy.json"
    _write_legacy(old, rec)

    cp = CheckpointStore(str(old)).load()
    assert len(cp["records"]) == 1
    assert set(cp["scanned_mids"]) == {"old1", "old2"}
    assert cp["last_run_ts"] == 555.0
    assert cp["stats"]["B微博数"] == 7
    assert cp["like_api_ok"] is False
    assert cp["migrated_count"] == 1, "迁移条数应暴露给调用方用于日志提示"


def test_legacy_json_file_is_kept_after_migration(cpdir: Path, rec: Rec) -> None:
    old = cpdir / "legacy.json"
    _write_legacy(old, rec)
    CheckpointStore(str(old)).load()
    assert old.exists(), "迁移必须是只读的，不能删用户的历史文件"


def test_second_open_does_not_migrate_again(cpdir: Path, rec: Rec) -> None:
    old = cpdir / "legacy.json"
    _write_legacy(old, rec)
    CheckpointStore(str(old)).load()

    again = CheckpointStore(str(old)).load()
    assert len(again["records"]) == 1, "迁移标记应写进 meta，避免每次打开都重复导入"
    assert again["migrated_count"] == 0


def test_corrupt_legacy_json_does_not_break_load(cpdir: Path) -> None:
    old = cpdir / "legacy.json"
    old.write_text("{ 这不是合法 JSON", encoding="utf-8")
    cp = CheckpointStore(str(old)).load()
    assert cp["records"] == []


# --------------------------------------------------------------------------- #
# 损坏容错
# --------------------------------------------------------------------------- #


def test_corrupt_db_file_returns_empty_structure(cpdir: Path) -> None:
    bad = cpdir / "broken.db"
    bad.write_bytes(b"not a sqlite file at all")
    cp = CheckpointStore(str(bad)).load()
    assert cp["records"] == []
    assert cp["scanned_mids"] == []


def test_corrupt_record_json_is_skipped(cpdir: Path) -> None:
    """单条记录 JSON 坏掉时跳过该条，不能让整个断点读不出来。"""
    st = CheckpointStore(str(cpdir / "cp.db"))
    st.save([{"_key": ["k"], "时间": "t"}], set(), 1.0, {}, True, [])
    con = st._connect()
    con.execute("INSERT OR REPLACE INTO records(k, data) VALUES('bad', '{{{')")
    con.commit()
    con.close()

    cp = CheckpointStore(str(cpdir / "cp.db")).load()
    assert len(cp["records"]) == 1
