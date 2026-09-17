"""测试辅助：记录工厂与伪造客户端。

单独成模块（而不是塞进 ``conftest.py``）是为了让各测试文件可以显式
``from _support import FakeClient``——比依赖 ``conftest`` 的模块名更稳。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from client import WeiboClient


def make_record(
    itype: str, direction: str, actor: str, wb_text: str, act_text: str, t: str = "2026-01-02 10:00:00"
) -> dict[str, Any]:
    """构造一条 ``InteractionRecord`` 测试数据。

    ``direction`` 取 ``"A→B"`` / ``"B→A"``，决定「微博作者」是谁。
    """
    return {
        "时间": t,
        "互动类型": itype,
        "方向": direction,
        "发起方": actor,
        "微博作者": "乙" if direction == "A→B" else "甲",
        "微博内容": wb_text,
        "互动内容": act_text,
        "被回复人": "",
        "被回复评论": "",
        "微博时间": "2026-01-01 09:00:00",
        "微博链接": "https://weibo.com/detail/1",
        "互动链接": "https://weibo.com/u/9",
        "_key": ["k", itype, direction, actor],
    }


class FakeClient(WeiboClient):
    """只替换 ``get_json`` 的客户端：记录每次请求，返回内容交给 handler 决定。

    故意不调用 ``WeiboClient.__init__``——测试既不需要真实 Session，
    也不需要令牌桶（限速另有独立用例，见 ``test_client.py``）。
    """

    workers = 1

    def __init__(self, handler: Callable[[str, dict[str, Any]], Any]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        retries: int = 3,
        headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append((url, dict(params or {})))
        return self.handler(url, params or {})

    def urls(self) -> list[str]:
        """本次已请求的 URL 列表（便于断言「请求了哪些接口」）。"""
        return [u for u, _ in self.calls]
