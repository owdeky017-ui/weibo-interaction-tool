"""``InteractionAnalyzer`` 测试：评论双排序剪枝、并发执行、风控上抛、去重键。

「评论双排序」是覆盖率与请求量的权衡点：剪枝条件写松了会漏掉折叠评论，
写紧了又白跑一遍热度序。这里把三种分支（已拿全 / 疑似有折叠 / 接口不给总数）
都固定下来，防止以后调优时改错方向。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from _support import FakeClient
from analyzer import InteractionAnalyzer, _reply_target_name
from client import RiskControlError

UserA = {"uid": "1", "screen_name": "甲"}
UserB = {"uid": "2", "screen_name": "乙"}

#: _record_key 的公共测试输入
WB = {"mid": "m1", "url": "u1"}


def _analyzer(client: FakeClient, types: set[str] | None = None) -> InteractionAnalyzer:
    return InteractionAnalyzer(client, UserA, UserB, types=types or {"评论"}, checkpoint=None)


def _comment(cid: str, uid: str = "2") -> dict[str, Any]:
    return {
        "uid": uid,
        "screen_name": "乙",
        "created_at": 1767000000.0,
        "text": f"c{cid}",
        "cid": str(cid),
        "reply_to": "",
        "reply_to_user": "",
        "is_reply": False,
        "inner": [],
    }


def _weibo(mid: str) -> dict[str, Any]:
    """``_add()`` 需要的字段：mid / text_plain / created_at / url。"""
    return {
        "mid": mid,
        "text_plain": "微博正文",
        "text_html": "微博正文",
        "created_at": 1767000000.0,
        "url": f"https://weibo.com/detail/{mid}",
        "reposts_count": 0,
        "comments_count": 0,
        "attitudes_count": 0,
    }


def _scan_with(client: FakeClient, mid: str = "m1") -> InteractionAnalyzer:
    a = _analyzer(client)
    a._scan_comments(_weibo(mid), "2", "乙", "A", "B", owner_uid="1")
    return a


# --------------------------------------------------------------------------- #
# 评论双排序剪枝
# --------------------------------------------------------------------------- #


class TestCommentFlowPruning:
    def test_skips_hot_flow_when_time_flow_already_complete(self) -> None:
        """时间序一次拿全（38 条 / total=38 / has_more=False）→ 跳过热度序。"""
        flows: list[str] = []

        def fetch(
            mid: str, page: int = 1, cid: str | None = None, flow: str = "1", bulletin: int = 0
        ) -> tuple[list[dict[str, Any]], bool, str, int]:
            flows.append(flow)
            return ([_comment(i) for i in range(38)], False, "", 38)

        client = FakeClient(lambda u, p: {})
        client.fetch_comments = fetch  # type: ignore[method-assign]
        a = _scan_with(client)
        assert flows == ["1"], "集合已完全一致时再跑热度序纯属浪费"
        assert len(a.records) == 38, "剪枝不能以漏数据为代价"

    def test_runs_hot_flow_when_time_flow_is_incomplete(self) -> None:
        """拿到 38 条但 total=100（疑似有折叠）→ 必须补跑热度序。"""
        flows: list[str] = []

        def fetch(
            mid: str, page: int = 1, cid: str | None = None, flow: str = "1", bulletin: int = 0
        ) -> tuple[list[dict[str, Any]], bool, str, int]:
            flows.append(flow)
            if page == 1 and flow == "1":
                return ([_comment(i) for i in range(38)], False, "", 100)
            if page == 1 and flow == "0":
                return ([_comment(100 + i) for i in range(5)], False, "", 100)
            return ([], False, "", 100)

        client = FakeClient(lambda u, p: {})
        client.fetch_comments = fetch  # type: ignore[method-assign]
        a = _scan_with(client, "m2")
        assert flows == ["1", "0"]
        assert len(a.records) == 43, "热度序独有的折叠评论也要收录"

    def test_runs_both_flows_when_total_unknown(self) -> None:
        """接口未返回 total_number → 保守跑双排序，不降低覆盖度。"""
        flows: list[str] = []

        def fetch(
            mid: str, page: int = 1, cid: str | None = None, flow: str = "1", bulletin: int = 0
        ) -> tuple[list[dict[str, Any]], bool, str, int]:
            flows.append(flow)
            if page == 1:
                return ([_comment(f"{flow}{i}") for i in range(10)], False, "", 0)
            return ([], False, "", 0)

        client = FakeClient(lambda u, p: {})
        client.fetch_comments = fetch  # type: ignore[method-assign]
        _scan_with(client, "m3")
        assert flows == ["1", "0"]

    def test_cid_deduplicates_across_flows(self) -> None:
        """两种排序返回重叠评论时按 cid 去重，不能重复计数。"""

        def fetch(
            mid: str, page: int = 1, cid: str | None = None, flow: str = "1", bulletin: int = 0
        ) -> tuple[list[dict[str, Any]], bool, str, int]:
            if page == 1:
                return ([_comment(i) for i in range(5)], False, "", 99)
            return ([], False, "", 99)

        client = FakeClient(lambda u, p: {})
        client.fetch_comments = fetch  # type: ignore[method-assign]
        a = _scan_with(client, "m4")
        assert len(a.records) == 5

    def test_pagination_follows_has_more(self) -> None:
        """has_more=True 时必须继续翻页，否则会丢后半段评论。"""
        pages: list[int] = []

        def fetch(
            mid: str, page: int = 1, cid: str | None = None, flow: str = "1", bulletin: int = 0
        ) -> tuple[list[dict[str, Any]], bool, str, int]:
            pages.append(page)
            if flow != "1":
                return ([], False, "", 0)
            if page == 1:
                return ([_comment(i) for i in range(20)], True, "", 40)
            if page == 2:
                return ([_comment(100 + i) for i in range(20)], False, "", 40)
            return ([], False, "", 40)

        client = FakeClient(lambda u, p: {})
        client.fetch_comments = fetch  # type: ignore[method-assign]
        a = _scan_with(client, "m5")
        assert pages == [1, 2]
        assert len(a.records) == 40

    def test_blacklist_tip_is_recorded(self) -> None:
        def fetch(
            mid: str, page: int = 1, cid: str | None = None, flow: str = "1", bulletin: int = 0
        ) -> tuple[list[dict[str, Any]], bool, str, int]:
            return ([], False, "对方已将你拉黑", 0)

        client = FakeClient(lambda u, p: {})
        client.fetch_comments = fetch  # type: ignore[method-assign]
        a = _scan_with(client, "m6")
        assert a.stats.get("拉黑提示") == "对方已将你拉黑"

    def test_comment_count_recorded_in_stats(self) -> None:
        def fetch(
            mid: str, page: int = 1, cid: str | None = None, flow: str = "1", bulletin: int = 0
        ) -> tuple[list[dict[str, Any]], bool, str, int]:
            return ([_comment(1), _comment(2)], False, "", 2)

        client = FakeClient(lambda u, p: {})
        client.fetch_comments = fetch  # type: ignore[method-assign]
        a = _scan_with(client, "m7")
        assert a.stats["评论可见数"] == 2
        assert a.stats["请求数"] == 1


# --------------------------------------------------------------------------- #
# 并发执行
# --------------------------------------------------------------------------- #


class TestRunTasks:
    @staticmethod
    def _analyzer_with_workers(workers: int) -> InteractionAnalyzer:
        client = FakeClient(lambda u, p: {})
        client.workers = workers
        return _analyzer(client, types={"转发"})

    def test_workers_taken_from_client(self) -> None:
        assert self._analyzer_with_workers(3).workers == 3

    def test_tasks_run_concurrently(self) -> None:
        a = self._analyzer_with_workers(3)
        sleep = 0.30
        t0 = time.monotonic()
        res = a._run_tasks(
            [
                ("t1", lambda: (time.sleep(sleep), "v1")[1], False),
                ("t2", lambda: (time.sleep(sleep), "v2")[1], False),
                ("t3", lambda: (time.sleep(sleep), "v3")[1], False),
            ]
        )
        parallel = time.monotonic() - t0
        assert parallel < sleep * 2, f"并发 {parallel * 1000:.0f}ms vs 串行 {sleep * 3 * 1000:.0f}ms"
        assert res == {"t1": ("v1", None), "t2": ("v2", None), "t3": ("v3", None)}

    def test_single_task_uses_serial_path(self) -> None:
        """单任务不启动线程池（避免无谓的池化开销）。"""
        a = self._analyzer_with_workers(3)
        t0 = time.monotonic()
        a._run_tasks([("only", lambda: "v", False)])
        assert (time.monotonic() - t0) < 0.05

    def test_empty_task_list(self) -> None:
        assert self._analyzer_with_workers(3)._run_tasks([]) == {}

    def test_risk_control_raised_in_calling_thread(self) -> None:
        """RiskControlError 必须回到调用线程上抛，保持「外层长等待后重试」语义。"""
        a = self._analyzer_with_workers(3)

        def boom() -> None:
            raise RiskControlError("触发了风控")

        with pytest.raises(RiskControlError, match="触发了风控"):
            a._run_tasks([("r", boom, False)])

    def test_ordinary_exception_returned_as_err(self) -> None:
        """普通异常不抛出，作为 err 返回，由调用方决定是否记为失败微博。"""
        a = self._analyzer_with_workers(3)

        def bad() -> None:
            raise ValueError("bad value")

        res = a._run_tasks([("e", bad, False)])
        assert res["e"][0] is None
        assert isinstance(res["e"][1], ValueError)

    def test_retry_wrapper_swallows_transient_error(self) -> None:
        """use_retry=True 时网络异常由 _retry 兜住，最多重试 attempts 次。"""
        a = self._analyzer_with_workers(1)
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("网络抖动")
            return "ok"

        res = a._run_tasks([("f", flaky, True)])
        assert res["f"] == ("ok", None)
        assert calls["n"] == 3

    def test_retry_gives_up_and_reports_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """重试耗尽后返回 (None, err)，并且不应真的 sleep 掉测试时间。"""
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        a = self._analyzer_with_workers(1)

        def always_fail() -> str:
            raise ConnectionError("一直失败")

        res = a._run_tasks([("f", always_fail, True)])
        assert res["f"][0] is None
        assert isinstance(res["f"][1], ConnectionError)

    def test_retry_does_not_catch_risk_control(self) -> None:
        """风控不能被 _retry 当成网络抖动重试——重试只会继续被拦。"""
        a = self._analyzer_with_workers(1)

        def risky() -> None:
            raise RiskControlError("限流")

        with pytest.raises(RiskControlError):
            a._run_tasks([("r", risky, True)])

    def test_failure_is_recorded_in_failed_weibos(self) -> None:
        a = self._analyzer_with_workers(1)
        a._record_fail("m1", _weibo("m1"), "转发", ValueError("x"))  # type: ignore[arg-type]
        assert len(a.failed_weibos) == 1
        assert a.failed_weibos[0]["mid"] == "m1"
        assert a.failed_weibos[0]["stage"] == "转发"
        assert "ValueError" in a.failed_weibos[0]["reason"]


# --------------------------------------------------------------------------- #
# 去重键
# --------------------------------------------------------------------------- #


class TestRecordKey:
    def test_comment_key_uses_cid(self) -> None:
        key = InteractionAnalyzer._record_key("评论", "A→B", WB, {"cid": "c1"})
        assert key == ("互动", "A→B", "c1")

    def test_comment_reply_key_uses_cid(self) -> None:
        key = InteractionAnalyzer._record_key("评论回复", "A→B", WB, {"cid": "c2"})
        assert key == ("互动", "A→B", "c2")

    def test_comment_without_cid_is_not_deduplicated(self) -> None:
        assert InteractionAnalyzer._record_key("评论", "A→B", WB, {}) is None

    def test_repost_key_uses_repost_mid(self) -> None:
        key = InteractionAnalyzer._record_key("转发", "A→B", WB, {"mid": "r1"})
        assert key == ("互动", "A→B", "r1")

    def test_repost_without_mid_is_not_deduplicated(self) -> None:
        assert InteractionAnalyzer._record_key("转发", "A→B", WB, {}) is None

    def test_like_key_uses_weibo_mid_and_uid(self) -> None:
        key = InteractionAnalyzer._record_key("点赞", "A→B", WB, {"uid": "9"})
        assert key == ("点赞", "m1", "9")

    def test_like_without_uid_is_not_deduplicated(self) -> None:
        assert InteractionAnalyzer._record_key("点赞", "A→B", WB, {}) is None

    def test_mention_key_uses_weibo_mid(self) -> None:
        key = InteractionAnalyzer._record_key("@提及", "A→B", WB, None)
        assert key == ("互动", "A→B", "m1")

    def test_unknown_type_is_not_deduplicated(self) -> None:
        assert InteractionAnalyzer._record_key("未知", "A→B", WB, None) is None

    def test_falls_back_to_url_when_mid_missing(self) -> None:
        key = InteractionAnalyzer._record_key("点赞", "A→B", {"url": "u9"}, {"uid": "1"})
        assert key == ("点赞", "u9", "1")


# --------------------------------------------------------------------------- #
# 楼中楼回复对象解析
# --------------------------------------------------------------------------- #


class TestReplyTargetName:
    @pytest.mark.parametrize(
        "text,expect",
        [
            ("回复@owdeky:厨师正在备餐", "owdeky"),
            ("回复@张三：你好", "张三"),
            ("  回复@李四:嗯", "李四"),
            ("普通评论", ""),
            ("", ""),
            (None, ""),
        ],
    )
    def test_parses_target(self, text: str | None, expect: str) -> None:
        assert _reply_target_name(text) == expect


# --------------------------------------------------------------------------- #
# _add：方向与去重
# --------------------------------------------------------------------------- #


class TestAddRecord:
    @staticmethod
    def _analyzer() -> InteractionAnalyzer:
        return _analyzer(FakeClient(lambda u, p: {}), types={"评论"})

    def test_mention_direction_is_owner_to_other(self) -> None:
        a = self._analyzer()
        a._add("A", "B", "@提及", _weibo("m1"), "@乙", None)  # type: ignore[arg-type]
        assert a.records[0]["方向"] == "A→B"
        assert a.records[0]["微博作者"] == "甲"

    def test_like_direction_is_other_to_owner(self) -> None:
        a = self._analyzer()
        a._add("A", "B", "点赞", _weibo("m1"), "点赞了这条微博", None)  # type: ignore[arg-type]
        assert a.records[0]["方向"] == "B→A"

    def test_explicit_direction_wins(self) -> None:
        a = self._analyzer()
        a._add(
            "A",
            "B",
            "评论回复",
            _weibo("m1"),
            "回复@乙:好",
            {"cid": "c1"},  # type: ignore[arg-type]
            direction="A→B",
        )
        assert a.records[0]["方向"] == "A→B"

    def test_duplicate_key_is_ignored(self) -> None:
        a = self._analyzer()
        actor = {"cid": "c1", "screen_name": "乙", "created_at": 1767000000.0}
        a._add("A", "B", "评论", _weibo("m1"), "内容", actor)  # type: ignore[arg-type]
        a._add("A", "B", "评论", _weibo("m1"), "内容", actor)  # type: ignore[arg-type]
        assert len(a.records) == 1

    def test_records_without_key_are_both_kept(self) -> None:
        a = self._analyzer()
        a._add("A", "B", "评论", _weibo("m1"), "内容", {})  # type: ignore[arg-type]
        a._add("A", "B", "评论", _weibo("m1"), "内容", {})  # type: ignore[arg-type]
        assert len(a.records) == 2
