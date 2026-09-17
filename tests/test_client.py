"""``WeiboClient`` 测试：翻页剪枝（命中即停）与令牌桶限速。

这两项是「少发请求」的核心优化，也是最容易在重构中被改坏的地方：
剪枝写错会**漏数据**（比多请求严重得多），限速写错会**触发风控**。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from _support import FakeClient
from client import RiskControlError, WeiboClient

TARGET = "9999"

Handler = Callable[[str, dict[str, Any]], Any]


# --------------------------------------------------------------------------- #
# 转发列表：命中当页即停
# --------------------------------------------------------------------------- #


def _repost_handler(hit_page: int | None, per_page: int = 20) -> Handler:
    """每页 per_page 条；命中页的第 4 条是目标用户。hit_page=None 表示永不命中。"""

    def handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        page = params.get("page", 1)
        items = []
        for i in range(per_page):
            uid = TARGET if (page == hit_page and i == 3) else f"u{page}_{i}"
            items.append(
                {
                    "user": {"id": uid, "screen_name": "x"},
                    "created_at": "Tue Jan 06 10:00:00 +0800 2026",
                    "text_raw": "t",
                    "idstr": f"m{page}_{i}",
                }
            )
        return {"data": items}

    return handler


class TestFetchRepostsPruning:
    def test_stops_at_first_page_when_target_found(self) -> None:
        fc = FakeClient(_repost_handler(hit_page=1))
        out = fc.fetch_reposts("mid1", target_uid=TARGET)
        assert len(fc.calls) == 1, "目标在第 1 页就不该继续翻剩下的 29 页"
        assert any(r["uid"] == TARGET for r in out)

    def test_without_target_uid_behaviour_unchanged(self) -> None:
        """不传 target_uid = 原行为（翻满 max_pages），保证向后兼容。"""
        fc = FakeClient(_repost_handler(hit_page=1))
        fc.fetch_reposts("mid1")
        assert len(fc.calls) == 30

    def test_missing_target_still_scans_every_page(self) -> None:
        """目标不存在时必须翻完，否则会漏掉「其实转发过」的情况。"""
        fc = FakeClient(_repost_handler(hit_page=None))
        fc.fetch_reposts("mid3", target_uid=TARGET)
        assert len(fc.calls) == 30

    def test_collects_all_hits_within_page(self) -> None:
        """同一页内可能有多条（重复转发），必须全部收齐再停。"""

        def handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
            if params.get("page") != 1:
                raise AssertionError("命中页后不应再请求第 2 页")
            return {
                "data": [
                    {
                        "user": {"id": TARGET, "screen_name": "x"},
                        "created_at": "Tue Jan 06 10:00:00 +0800 2026",
                        "text_raw": "t",
                        "idstr": f"m{i}",
                    }
                    for i in range(20)
                ]
            }

        fc = FakeClient(handler)
        out = fc.fetch_reposts("mid", target_uid=TARGET)
        assert len(out) == 20
        assert len(fc.calls) == 1

    def test_short_page_ends_pagination(self) -> None:
        fc = FakeClient(_repost_handler(hit_page=None, per_page=5))
        fc.fetch_reposts("mid")
        assert len(fc.calls) == 1, "不足 20 条的页 = 最后一页"

    def test_empty_page_ends_pagination(self) -> None:
        fc = FakeClient(lambda u, p: {"data": []})
        assert fc.fetch_reposts("mid") == []
        assert len(fc.calls) == 1

    def test_dict_shaped_data_is_unwrapped(self) -> None:
        """接口偶尔返回 {"data": {"list": [...]}}，需要兼容。"""
        fc = FakeClient(
            lambda u, p: {
                "data": {
                    "list": [
                        {
                            "user": {"id": "1", "screen_name": "a"},
                            "created_at": "???",
                            "text_raw": "t",
                            "idstr": "m1",
                        }
                    ]
                }
            }
        )
        assert len(fc.fetch_reposts("mid")) == 1

    def test_items_without_user_are_skipped(self) -> None:
        fc = FakeClient(
            lambda u, p: {
                "data": [
                    {"created_at": "???", "text_raw": "t"},
                    {
                        "user": {"id": "7", "screen_name": "a"},
                        "created_at": "???",
                        "text_raw": "t",
                        "idstr": "m1",
                    },
                ]
            }
        )
        assert [r["uid"] for r in fc.fetch_reposts("mid")] == ["7"]

    def test_target_uid_compared_as_string(self) -> None:
        """接口返回的 uid 可能是 int，与 str 形式的 target_uid 比较时必须归一。"""
        fc = FakeClient(
            lambda u, p: {
                "data": [
                    {
                        "user": {"id": 9999, "screen_name": "x"},
                        "created_at": "???",
                        "text_raw": "t",
                        "idstr": "m1",
                    }
                ]
            }
        )
        out = fc.fetch_reposts("mid", target_uid=TARGET)
        assert [r["uid"] for r in out] == [TARGET]


# --------------------------------------------------------------------------- #
# 点赞列表：命中当页即停
# --------------------------------------------------------------------------- #


def _attitude_handler(hit_page: int | None, per_page: int = 50) -> Handler:
    def handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        page = params.get("page", 1)
        items = []
        for i in range(per_page):
            uid = TARGET if (page == hit_page and i == 7) else f"a{page}_{i}"
            items.append(
                {"user": {"id": uid, "screen_name": "y"}, "created_at": "Tue Jan 06 10:00:00 +0800 2026"}
            )
        return {"ok": 1, "data": {"data": items}}

    return handler


class TestFetchAttitudesPruning:
    def test_stops_at_hit_page(self) -> None:
        fc = FakeClient(_attitude_handler(hit_page=2))
        out = fc.fetch_attitudes("mid2", target_uid=TARGET)
        assert len(fc.calls) == 2, "目标在第 2 页，不该翻满 18 页"
        assert any(r["uid"] == TARGET for r in out)

    def test_without_target_uid_behaviour_unchanged(self) -> None:
        fc = FakeClient(_attitude_handler(hit_page=2))
        fc.fetch_attitudes("mid2")
        assert len(fc.calls) == 18

    def test_ok_not_1_stops_immediately(self) -> None:
        """接口返回 ok != 1 表示不可用，直接放弃而不是继续翻。"""
        fc = FakeClient(lambda u, p: {"ok": 0, "data": {}})
        assert fc.fetch_attitudes("mid") == []
        assert len(fc.calls) == 1

    def test_risk_control_is_reraised(self) -> None:
        """点赞接口的风控必须上抛（由上层长等待后重试），不能当「接口不可用」吞掉。"""

        def handler(url: str, params: dict[str, Any]) -> Any:
            raise RiskControlError("被限流")

        fc = FakeClient(handler)
        with pytest.raises(RiskControlError):
            fc.fetch_attitudes("mid")

    def test_other_errors_stop_quietly(self) -> None:
        """点赞是「尽力而为」：接口挂了不应中断整轮扫描。"""

        def handler(url: str, params: dict[str, Any]) -> Any:
            raise ValueError("接口结构变了")

        fc = FakeClient(handler)
        assert fc.fetch_attitudes("mid") == []
        assert len(fc.calls) == 1

    def test_request_uses_mobile_referer(self) -> None:
        """点赞走移动端接口，Referer 必须带上，否则容易被拦。"""
        seen: dict[str, Any] = {}

        def handler(url: str, params: dict[str, Any]) -> Any:
            seen["url"] = url
            return {"ok": 0}

        FakeClient(handler).fetch_attitudes("mid")
        assert "m.weibo.cn" in seen["url"]


# --------------------------------------------------------------------------- #
# 令牌桶限速
# --------------------------------------------------------------------------- #


class TestTokenBucket:
    @staticmethod
    def _client(interval: float = 0.02, burst: float = 2.0) -> WeiboClient:
        c = WeiboClient(object(), speed=2)  # type: ignore[arg-type]
        c.min_interval = interval
        c._rate = 1.0 / interval
        c._burst = burst
        c._tokens = burst
        c._last_refill = time.monotonic()
        return c

    def test_burst_requests_do_not_wait(self) -> None:
        """桶里已有令牌时不应等待——这正是相对「每次固定 sleep」的收益。"""
        c = self._client()
        t0 = time.monotonic()
        c._throttle()
        c._throttle()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.02, f"突发额度内等待了 {elapsed * 1000:.1f}ms"

    def test_ten_requests_take_expected_time(self) -> None:
        c = self._client()
        t0 = time.monotonic()
        for _ in range(10):
            c._throttle()
        elapsed = time.monotonic() - t0
        expect = 8 * 0.02  # 10 次请求 - 2 个突发令牌 = 需补 8 个
        assert expect * 0.6 <= elapsed <= expect * 2.2 + 0.05, (
            f"实测 {elapsed * 1000:.1f}ms，理论 {expect * 1000:.1f}ms"
        )

    def test_average_qps_never_exceeds_configured_rate(self) -> None:
        c = self._client()
        t0 = time.monotonic()
        for _ in range(10):
            c._throttle()
        elapsed = time.monotonic() - t0
        rate = 10 / elapsed
        assert rate <= (1 / 0.02) * 1.35, f"实测 {rate:.1f} QPS，配置上限 {1 / 0.02:.1f}"

    def test_burst_beats_fixed_sleep(self) -> None:
        """固定 sleep 方案前 2 次也要各等满一个间隔；令牌桶应显著更快。"""
        c = self._client()
        t0 = time.monotonic()
        c._throttle()
        c._throttle()
        burst_elapsed = time.monotonic() - t0
        assert burst_elapsed < 2 * 0.02 * 0.5

    def test_tokens_never_exceed_burst_capacity(self) -> None:
        """长时间空闲后不能攒出超过桶容量的令牌（否则会打出大突发）。"""
        c = self._client()
        c._last_refill -= 10.0  # 假装空闲了 10 秒
        c._throttle()
        assert c._tokens <= c._burst

    def test_zero_interval_does_not_divide_by_zero(self) -> None:
        c = WeiboClient(object(), speed=2)  # type: ignore[arg-type]
        c.min_interval = 0.0
        c._rate = 1e9
        c._burst = 1.0
        c._tokens = 1.0
        c._last_refill = time.monotonic()
        c._throttle()  # 不抛异常即通过

    def test_speed_map_is_honoured(self) -> None:
        """三档速度必须映射到三个依次递减的请求间隔。"""
        intervals = {
            s: WeiboClient(object(), speed=s).min_interval  # type: ignore[arg-type]
            for s in (1, 2, 3)
        }
        assert intervals[1] > intervals[2] > intervals[3]

    def test_workers_default_from_speed_map(self) -> None:
        assert WeiboClient(object(), speed=1).workers == 1  # type: ignore[arg-type]

    def test_explicit_workers_override(self) -> None:
        assert WeiboClient(object(), speed=1, workers=5).workers == 5  # type: ignore[arg-type]
