"""领域模型：全工程共用的数据结构定义（纯类型，无运行时逻辑、无第三方依赖）。

集中在一处的理由：这些结构是「微博接口 JSON → 内部表示 → 导出」这条链路上的
公共契约。原先它们散落在 ``fetchers`` / ``client`` / ``analyzer`` 里，
导致 ``exporter`` 想标注记录类型就得反过来 import ``analyzer``（导出依赖分析，
方向是反的）。抽出来之后依赖方向变成单向：

    models ← fetchers ← client ← analyzer ← exporter / main / gui_app

运行时它们都是普通 ``dict``，没有任何额外开销——TypedDict 只在类型检查期生效。
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class WeiboItem(TypedDict):
    """``parse_weibo_item`` 的统一输出结构（mymblog 接口 → 内部表示）。"""

    mid: str
    mblogid: str
    created_at: float | None
    text_html: str
    text_plain: str
    reposts_count: int
    comments_count: int
    attitudes_count: int
    url: str


class Mention(TypedDict):
    """一条 @提及。``uid`` 为 ``None`` 表示只有昵称、没有可解析的 uid。"""

    uid: str | None
    screen_name: str


class UserInfo(TypedDict):
    """一个微博用户的最小标识。"""

    uid: str
    screen_name: str


class InnerComment(TypedDict):
    """评论里内嵌的楼中楼回复（接口返回的摘要，可能不全）。"""

    uid: str
    screen_name: str
    created_at: float | None
    text: str


class CommentItem(TypedDict):
    """一条评论或楼中楼回复（``fetch_comments`` / ``fetch_approval_comments`` 的统一结构）。"""

    uid: str
    screen_name: str
    created_at: float | None
    text: str
    cid: str
    reply_to: str
    reply_to_user: str
    is_reply: bool
    inner: list[InnerComment]
    approval: NotRequired[bool]


class RepostItem(TypedDict):
    """一条转发记录。"""

    uid: str
    screen_name: str
    created_at: float | None
    text: str
    mid: str


class AttitudeItem(TypedDict):
    """一条点赞记录。"""

    uid: str
    screen_name: str
    created_at: float | None


class InteractionRecord(TypedDict):
    """一条互动记录（导出的最小单位）。``_key`` 为 ``None`` 表示该类型不去重。"""

    _key: list[str] | None
    时间: str
    互动类型: str
    方向: str
    发起方: str
    微博作者: str
    微博内容: str
    互动内容: str
    被回复人: str
    被回复评论: str
    微博时间: str
    微博链接: str
    互动链接: str


class FailedWeibo(TypedDict):
    """一条「重试后仍失败」的微博，导出时会在 HTML 顶部提示。"""

    mid: str
    url: str
    stage: str
    reason: str


class CheckpointData(TypedDict):
    """断点文件的完整内容（``CheckpointStore.load()`` 的返回结构）。"""

    records: list[dict[str, Any]]
    scanned_mids: list[str]
    last_run_ts: float
    stats: dict[str, Any]
    like_api_ok: bool | None
    failed_weibos: list[dict[str, Any]]
    migrated_count: int
