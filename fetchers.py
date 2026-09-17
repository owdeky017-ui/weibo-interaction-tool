"""数据解析：微博条目解析、@提及提取。

纯函数模块，不涉及网络与全局状态，因此是单元测试的重点覆盖对象
（见 ``tests/test_fetchers.py``）。
"""

from __future__ import annotations

import html
import re
from typing import Any

from models import Mention, WeiboItem
from utils import parse_weibo_time

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str:
    """HTML 微博文本 → 纯文本（<br/> 转空格）。"""
    if not text:
        return ""
    text = text.replace("<br />", " ").replace("<br/>", " ")
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def parse_weibo_item(item: dict[str, Any]) -> WeiboItem:
    """mymblog 接口的单条微博 → 统一结构。"""
    text_html = item.get("text") or ""
    return {
        "mid": str(item.get("idstr") or item.get("id") or ""),
        "mblogid": item.get("mblogid") or "",
        "created_at": parse_weibo_time(item.get("created_at")),
        "text_html": text_html,
        "text_plain": strip_html(text_html),
        "reposts_count": item.get("reposts_count") or 0,
        "comments_count": item.get("comments_count") or 0,
        "attitudes_count": item.get("attitudes_count") or 0,
        "url": f"https://weibo.com/detail/{item.get('mblogid') or item.get('idstr') or ''}",
    }


# @ 的 HTML 形态：
#   <a href="//weibo.com/u/12345?refer_flag=...">@昵称</a>
#   <a href="//weibo.com/n/昵称?refer_flag=...">@昵称</a>
_AT_RE = re.compile(r'<a[^>]+href="//weibo\.com/(?:u/(\d+)|n/([^"?]+))[^"]*"[^>]*>(@[^<]+)</a>')
_PLAIN_AT_RE = re.compile(r"(@[\u4e00-\u9fa5A-Za-z0-9_\-·]{1,30})")
# 完整 <a> 标签（用于剔除后找纯文本 @，避免重复计数）
_A_TAG_RE = re.compile(r"<a\s+[^>]*>.*?</a>", re.S)


def extract_mentions(text_html: str | None) -> list[Mention]:
    """从微博 HTML 文本中提取 @ 的对象列表。

    先解析带链接的 ``<a>`` 形态（能拿到 uid），再把 ``<a>`` 标签剔除后
    用纯文本正则补充无链接的 @（只有昵称）。同一条微博内的重复提及会去重。
    """
    mentions: list[Mention] = []
    seen: set[str] = set()
    for m in _AT_RE.finditer(text_html or ""):
        uid, name, label = m.group(1), m.group(2), m.group(3)
        name = name or label.lstrip("@")
        name = html.unescape(name)
        key = uid or name
        if key in seen:
            continue
        seen.add(key)
        mentions.append({"uid": uid, "screen_name": name})
    # 剔除 <a> 链接后，再补充纯文本形态的 @（无链接时）
    no_links = _A_TAG_RE.sub(" ", text_html or "")
    for m in _PLAIN_AT_RE.finditer(no_links):
        name = m.group(1).lstrip("@")
        if name and name not in seen:
            seen.add(name)
            mentions.append({"uid": None, "screen_name": name})
    return mentions


def mentions_target(mentions: list[Mention], target_uid: str, target_name: str) -> bool:
    """提及列表是否命中目标用户（uid 精确匹配，或昵称归一化匹配）。"""
    if not mentions:
        return False

    def norm(s: str | None) -> str:
        return re.sub(r"[\s\u200b]+", "", s or "").lower()

    tu, tn = str(target_uid), norm(target_name)
    for m in mentions:
        if m.get("uid") and str(m["uid"]) == tu:
            return True
        if tn and m.get("screen_name") and norm(m["screen_name"]) == tn:
            return True
    return False
