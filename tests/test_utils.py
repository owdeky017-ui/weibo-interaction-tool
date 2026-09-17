"""``utils`` 纯函数测试。

微博接口返回的时间格式并不统一（相对时间 / 月-日 / 标准格式混用），
解析失败必须返回 ``None`` 而不是抛异常——抓取流程对单条脏数据是容错的。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from utils import CST, fmt_cst, parse_date_input, parse_weibo_time


class TestParseWeiboTime:
    @pytest.mark.parametrize("raw", [None, "", "   ", "不是时间", "2026/01/02", "25-13-45"])
    def test_unparsable_returns_none(self, raw: str | None) -> None:
        """解析不出来时返回 None，调用方据此跳过该条而不是中断整轮扫描。"""
        assert parse_weibo_time(raw) is None

    def test_just_now(self) -> None:
        before = datetime.now(CST).timestamp()
        ts = parse_weibo_time("刚刚")
        assert ts is not None
        assert before - 1 <= ts <= datetime.now(CST).timestamp() + 1

    @pytest.mark.parametrize("text,minutes", [("30分钟前", 30), ("1分钟前", 1), ("999分钟前", 999)])
    def test_minutes_ago(self, text: str, minutes: int) -> None:
        expect = (datetime.now(CST) - timedelta(minutes=minutes)).timestamp()
        assert abs((parse_weibo_time(text) or 0) - expect) < 5

    @pytest.mark.parametrize("text,hours", [("2小时前", 2), ("23小时前", 23)])
    def test_hours_ago(self, text: str, hours: int) -> None:
        expect = (datetime.now(CST) - timedelta(hours=hours)).timestamp()
        assert abs((parse_weibo_time(text) or 0) - expect) < 5

    def test_today_with_time(self) -> None:
        ts = parse_weibo_time("今天 08:30")
        assert ts is not None
        dt = datetime.fromtimestamp(ts, CST)
        assert dt.date() == datetime.now(CST).date()
        assert (dt.hour, dt.minute, dt.second) == (8, 30, 0)

    def test_yesterday_with_time(self) -> None:
        ts = parse_weibo_time("昨天 23:59")
        assert ts is not None
        dt = datetime.fromtimestamp(ts, CST)
        assert dt.date() == (datetime.now(CST) - timedelta(days=1)).date()
        assert (dt.hour, dt.minute) == (23, 59)

    def test_ymd_is_cst_midnight(self) -> None:
        assert parse_weibo_time("2026-01-02") == datetime(2026, 1, 2, tzinfo=CST).timestamp()

    def test_standard_weibo_format(self) -> None:
        """标准格式：Fri Sep 08 10:00:00 +0800 2025（星期几被忽略，直接按 CST 构造）。"""
        assert (
            parse_weibo_time("Fri Sep 08 10:00:00 +0800 2025")
            == datetime(2025, 9, 8, 10, 0, 0, tzinfo=CST).timestamp()
        )

    def test_standard_format_all_months_recognised(self) -> None:
        for idx, mon in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
        ):
            raw = f"Mon {mon} 01 00:00:00 +0800 2026"
            assert parse_weibo_time(raw) == datetime(2026, idx, 1, tzinfo=CST).timestamp()

    @pytest.mark.parametrize("raw", ["01-01", "06-15", "12-31"])
    def test_monthday_keeps_month_and_day(self, raw: str) -> None:
        """月-日形式：年份由「是否已过去」推断，但月/日必须原样保留。"""
        ts = parse_weibo_time(raw)
        assert ts is not None
        dt = datetime.fromtimestamp(ts, CST)
        assert (dt.month, dt.day) == (int(raw[:2]), int(raw[3:]))

    def test_monthday_never_in_the_future(self) -> None:
        """当前年的同一日期还没到时，应回退到去年（否则会出现未来时间戳）。"""
        now = datetime.now(CST)
        for raw in ("01-01", "06-15", "12-31"):
            ts = parse_weibo_time(raw)
            assert ts is not None
            assert datetime.fromtimestamp(ts, CST) <= now

    def test_monthday_rolls_back_when_date_is_ahead(self) -> None:
        now = datetime.now(CST)
        future = now + timedelta(days=40)
        raw = f"{future.month:02d}-{future.day:02d}"
        ts = parse_weibo_time(raw)
        assert ts is not None
        dt = datetime.fromtimestamp(ts, CST)
        assert (dt.month, dt.day) == (future.month, future.day)
        assert dt <= now

    def test_surrounding_whitespace_tolerated(self) -> None:
        assert parse_weibo_time("  2026-01-02  ") == parse_weibo_time("2026-01-02")


class TestFmtCst:
    def test_none_returns_empty_string(self) -> None:
        assert fmt_cst(None) == ""

    def test_formats_in_cst(self) -> None:
        ts = datetime(2026, 1, 2, 3, 4, 5, tzinfo=CST).timestamp()
        assert fmt_cst(ts) == "2026-01-02 03:04:05"

    def test_utc_input_is_converted_to_cst(self) -> None:
        ts = datetime(2026, 1, 1, 16, 0, 0, tzinfo=UTC).timestamp()
        assert fmt_cst(ts) == "2026-01-02 00:00:00"

    def test_round_trip_with_parse(self) -> None:
        assert fmt_cst(parse_weibo_time("2026-01-02")) == "2026-01-02 00:00:00"


class TestParseDateInput:
    def test_date_only(self) -> None:
        assert parse_date_input("2026-01-02") == datetime(2026, 1, 2, tzinfo=CST).timestamp()

    def test_date_with_time(self) -> None:
        assert parse_date_input("2026-01-02 13:45") == datetime(2026, 1, 2, 13, 45, tzinfo=CST).timestamp()

    def test_whitespace_is_stripped(self) -> None:
        assert parse_date_input("  2026-01-02  ") == parse_date_input("2026-01-02")

    @pytest.mark.parametrize("bad", ["", "2026/01/02", "01-02", "2026-13-01", "2026-01-02 25:00"])
    def test_invalid_input_raises_value_error(self, bad: str) -> None:
        """用户手填的日期非法时直接抛 ValueError，由 GUI 层提示重填。"""
        with pytest.raises(ValueError):
            parse_date_input(bad)
