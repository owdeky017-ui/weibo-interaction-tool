"""``fetchers`` 纯函数测试：HTML 清洗、微博条目解析、@提及提取与匹配。

这一组是「接口 JSON → 内部表示」的转换层，出错会直接污染所有导出结果，
因此是单元测试的重点覆盖对象。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from fetchers import extract_mentions, mentions_target, parse_weibo_item, strip_html
from utils import CST

#: 带 uid 的 @提及样例，供 mentions_target 复用
LINKED_MENTION = [{"uid": "12345", "screen_name": "张三"}]


class TestStripHtml:
    @pytest.mark.parametrize("raw", [None, ""])
    def test_empty_input(self, raw: str | None) -> None:
        assert strip_html(raw) == ""

    def test_removes_tags_keeps_text(self) -> None:
        assert strip_html('<a href="#">张三</a> 说：<b>你好</b>') == "张三 说：你好"

    @pytest.mark.parametrize("raw", ["第一行<br />第二行", "第一行<br/>第二行"])
    def test_br_becomes_space(self, raw: str) -> None:
        assert strip_html(raw) == "第一行 第二行"

    def test_unescapes_entities(self) -> None:
        assert strip_html("&lt;tag&gt; &amp; &quot;引号&quot;") == '<tag> & "引号"'

    def test_strips_surrounding_whitespace(self) -> None:
        assert strip_html("  <p>内容</p>  ") == "内容"

    def test_nested_tags(self) -> None:
        assert strip_html('<span class="x"><a href="#">@甲</a></span>') == "@甲"


class TestParseWeiboItem:
    def test_full_item(self) -> None:
        item = {
            "idstr": "5001",
            "mblogid": "Pabc123",
            "created_at": "Fri Sep 08 10:00:00 +0800 2025",
            "text": "<a href='#'>@张三</a> 你好<br/>世界",
            "reposts_count": 3,
            "comments_count": 4,
            "attitudes_count": 5,
        }
        wb = parse_weibo_item(item)
        assert wb["mid"] == "5001"
        assert wb["mblogid"] == "Pabc123"
        assert wb["created_at"] == datetime(2025, 9, 8, 10, 0, 0, tzinfo=CST).timestamp()
        assert wb["text_html"] == "<a href='#'>@张三</a> 你好<br/>世界"
        assert wb["text_plain"] == "@张三 你好 世界"
        assert (wb["reposts_count"], wb["comments_count"], wb["attitudes_count"]) == (3, 4, 5)
        assert wb["url"] == "https://weibo.com/detail/Pabc123"

    def test_missing_fields_get_safe_defaults(self) -> None:
        wb = parse_weibo_item({})
        assert wb["mid"] == ""
        assert wb["mblogid"] == ""
        assert wb["created_at"] is None
        assert wb["text_html"] == ""
        assert wb["text_plain"] == ""
        assert wb["reposts_count"] == 0
        assert wb["comments_count"] == 0
        assert wb["attitudes_count"] == 0
        assert wb["url"] == "https://weibo.com/detail/"

    def test_falls_back_to_id_when_idstr_missing(self) -> None:
        assert parse_weibo_item({"id": 7002, "text": "x"})["mid"] == "7002"

    def test_url_prefers_mblogid(self) -> None:
        wb = parse_weibo_item({"idstr": "1", "mblogid": "Pxyz", "text": "x"})
        assert wb["url"] == "https://weibo.com/detail/Pxyz"

    def test_unparsable_created_at_is_none_not_crash(self) -> None:
        assert parse_weibo_item({"idstr": "1", "created_at": "???"})["created_at"] is None

    def test_zero_count_is_not_treated_as_missing(self) -> None:
        wb = parse_weibo_item({"idstr": "1", "reposts_count": 0, "comments_count": 0})
        assert wb["reposts_count"] == 0
        assert wb["comments_count"] == 0


class TestExtractMentions:
    @pytest.mark.parametrize("raw", [None, ""])
    def test_empty_input(self, raw: str | None) -> None:
        assert extract_mentions(raw) == []

    def test_linked_mention_keeps_uid(self) -> None:
        html = '<a href="//weibo.com/u/12345?refer_flag=1001030103_">@张三</a>'
        assert extract_mentions(html) == [{"uid": "12345", "screen_name": "张三"}]

    def test_nickname_link_form(self) -> None:
        html = '<a href="//weibo.com/n/李四?refer_flag=1">@李四</a>'
        assert extract_mentions(html) == [{"uid": None, "screen_name": "李四"}]

    def test_plain_text_mention_without_link(self) -> None:
        assert extract_mentions("你好 @张三 和 @李四") == [
            {"uid": None, "screen_name": "张三"},
            {"uid": None, "screen_name": "李四"},
        ]

    def test_linked_mention_not_counted_twice(self) -> None:
        """带链接的 @ 在剔除 ``<a>`` 后不应被纯文本正则再抓一遍。"""
        html = '<a href="//weibo.com/u/1">@甲</a> 和 @乙'
        assert extract_mentions(html) == [
            {"uid": "1", "screen_name": "甲"},
            {"uid": None, "screen_name": "乙"},
        ]

    def test_duplicate_mentions_deduplicated(self) -> None:
        html = '<a href="//weibo.com/u/1">@甲</a><a href="//weibo.com/u/1">@甲</a>'
        assert len(extract_mentions(html)) == 1

    def test_duplicate_plain_mentions_deduplicated(self) -> None:
        assert len(extract_mentions("@甲 @甲")) == 1

    def test_html_entities_in_name_unescaped(self) -> None:
        html = '<a href="//weibo.com/u/1">@A&amp;B</a>'
        assert extract_mentions(html) == [{"uid": "1", "screen_name": "A&B"}]

    def test_name_with_middle_dot_and_dash(self) -> None:
        """昵称里的「·」和「-」是微博允许的字符，不能截断。"""
        assert extract_mentions("@A·B-C 你好") == [{"uid": None, "screen_name": "A·B-C"}]


class TestMentionsTarget:
    def test_empty_list_is_false(self) -> None:
        assert mentions_target([], "12345", "张三") is False

    def test_uid_match_wins_over_name(self) -> None:
        assert mentions_target(LINKED_MENTION, "12345", "完全不同的昵称") is True

    def test_name_match_when_uid_unknown(self) -> None:
        assert mentions_target([{"uid": None, "screen_name": "张三"}], "999", "张三") is True

    def test_name_match_normalises_spaces_and_zero_width(self) -> None:
        m = [{"uid": None, "screen_name": "张\u200b三"}]
        assert mentions_target(m, "999", "张 三") is True

    def test_name_match_is_case_insensitive(self) -> None:
        assert mentions_target([{"uid": None, "screen_name": "Alice"}], "999", "alice") is True

    def test_no_match(self) -> None:
        assert mentions_target(LINKED_MENTION, "999", "李四") is False

    def test_uid_mismatch_and_name_mismatch(self) -> None:
        m = [{"uid": "111", "screen_name": "王五"}]
        assert mentions_target(m, "999", "李四") is False

    def test_empty_target_name_does_not_match_by_name(self) -> None:
        """目标昵称为空时不能因为「两边都空」而误判命中。"""
        assert mentions_target([{"uid": None, "screen_name": ""}], "999", "") is False
