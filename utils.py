"""通用工具：微博时间解析。

微博接口返回的时间格式并不统一（相对时间、月-日、标准格式混用），
这里统一归一成 UTC 时间戳（秒）。解析不出来时返回 ``None`` 而不是抛异常——
抓取流程对单条时间解析失败是容错的（跳过该条），不应该因为一条脏数据中断整轮扫描。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# 微博时间均为东八区
CST = timezone(timedelta(hours=8))

#: (正则, 类型标记) —— 按顺序尝试匹配，第一个命中的决定解析方式
_REL_PATTERNS: list[tuple[str, str]] = [
    (r"^刚刚$", "just"),
    (r"^(\d+)分钟前$", "min"),
    (r"^(\d+)小时前$", "hour"),
    (r"^今天\s*(\d{1,2}):(\d{2})$", "today"),
    (r"^昨天\s*(\d{1,2}):(\d{2})$", "yesterday"),
    (r"^(\d{1,2})-(\d{1,2})$", "monthday"),
    (r"^(\d{4})-(\d{1,2})-(\d{1,2})$", "ymd"),
    # 标准格式：Fri Sep 08 10:00:00 +0800 2025
    (r"^[A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\+0800\s+\d{4}$", "std"),
]

_MONTH_MAP: dict[str, int] = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def parse_weibo_time(s: str | None) -> float | None:
    """把微博接口返回的时间字符串解析为 UTC 时间戳（秒）。

    支持：刚刚 / N分钟前 / N小时前 / 今天 HH:MM / 昨天 HH:MM /
          MM-DD / YYYY-MM-DD / 'Fri Sep 08 10:00:00 +0800 2025'

    无法识别或输入为空时返回 ``None``（调用方据此跳过该条）。
    """
    if not s:
        return None
    s = s.strip()
    now_cst = datetime.now(CST)

    for pat, kind in _REL_PATTERNS:
        m = re.match(pat, s)
        if not m:
            continue
        if kind == "just":
            return now_cst.timestamp()
        if kind == "min":
            return (now_cst - timedelta(minutes=int(m.group(1)))).timestamp()
        if kind == "hour":
            return (now_cst - timedelta(hours=int(m.group(1)))).timestamp()
        if kind == "today":
            dt = now_cst.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return dt.timestamp()
        if kind == "yesterday":
            dt = (now_cst - timedelta(days=1)).replace(
                hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
            )
            return dt.timestamp()
        if kind == "monthday":
            month, day = int(m.group(1)), int(m.group(2))
            year = now_cst.year
            dt = datetime(year, month, day, tzinfo=CST)
            if dt > now_cst:  # 未来日期说明是去年
                dt = datetime(year - 1, month, day, tzinfo=CST)
            return dt.timestamp()
        if kind == "ymd":
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=CST)
            return dt.timestamp()
        if kind == "std":
            # 忽略星期，直接按 CST 构造
            parts = s.split()
            hh, mm, ss = parts[3].split(":")
            dt = datetime(
                int(parts[5]), _MONTH_MAP[parts[1]], int(parts[2]), int(hh), int(mm), int(ss), tzinfo=CST
            )
            return dt.timestamp()
    return None


def fmt_cst(ts: float | None) -> str:
    """时间戳 → 东八区可读字符串；``None`` 返回空串。"""
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:%M:%S")


def parse_date_input(s: str) -> float:
    """解析用户输入的日期（YYYY-MM-DD 或 YYYY-MM-DD HH:MM），按东八区解释。"""
    s = s.strip()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%d")
    return dt.replace(tzinfo=CST).timestamp()
