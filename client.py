"""微博网页版 API 客户端：请求封装、限速、风控处理、用户解析。

并发模型
--------
本类可以安全地被多个线程同时调用（``analyzer`` 的线程池就是这么用的）：

* 限速用**令牌桶**保护：平均 QPS 由 ``config.SPEED_MAP`` 决定，桶容量等于
  worker 数，因此允许「最多 workers 个请求同时在途」的短突发，但长期速率不变；
* 每个线程通过 ``threading.local`` 拿到自己的 ``requests.Session``
  （``requests.Session`` 的 cookie jar 并非为多线程共享而设计）；
* ``request_count`` 的累加用独立锁保护。

风控与登录态
------------
微博的风控表现是 HTTP 418/403 或业务码 ``ok == -100``，登录态失效是 302/401/414。
前者抛 :class:`RiskControlError`（由上层长等待后重试），后者抛 :class:`NotLoggedInError`
（需要重新扫码）。两者都是**可预期**的失败路径，不当作 bug。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import requests

import config
from fetchers import parse_weibo_item
from models import (
    AttitudeItem,
    CommentItem,
    InnerComment,
    RepostItem,
    UserInfo,
    WeiboItem,
)
from utils import parse_weibo_time


class NotLoggedInError(Exception):
    """登录态失效。"""


class RiskControlError(Exception):
    """触发微博风控（频率过高/需要验证）。"""


class UserResolveError(Exception):
    """无法解析用户。"""


class WeiboClient:
    """带限速与重试的微博 API 客户端。"""

    def __init__(self, session: requests.Session, speed: int = 2, workers: int | None = None) -> None:
        self.base_session = session
        self.session = session  # 兼容外部直接读取
        self.min_interval = config.SPEED_MAP.get(speed, 0.6)
        self.workers = workers if workers is not None else config.SPEED_WORKERS.get(speed, 1)

        # 令牌桶限速：把「每次请求前固定 sleep min_interval」换成
        # 「按平均速率放行 + 允许最多 workers 个请求的短突发」。
        # 平均 QPS 与原来完全一致（仍然由 SPEED_MAP 决定），
        # 但连续请求时不再每次都白等一个完整间隔。
        self._rate = (1.0 / self.min_interval) if self.min_interval > 0 else 1e9
        self._burst = float(max(1, self.workers))
        self._tokens = self._burst
        self._last_refill = time.monotonic()
        self._rate_lock = threading.Lock()

        # 并发时每个线程用各自的 Session（requests.Session 的 cookie jar
        # 并非为多线程共享设计），登录态 cookie 从 base_session 复制。
        self._local = threading.local()

        # 请求计数（供 stats 之外的粗略观测）
        self.request_count = 0
        self._count_lock = threading.Lock()

    # ---------- 请求 ----------

    def _session(self) -> requests.Session:
        """当前线程专属 Session；workers<=1 时直接用原 Session，不做复制。"""
        s: requests.Session | None = getattr(self._local, "session", None)
        if s is None:
            if self.workers <= 1:
                s = self.base_session
            else:
                s = requests.Session()
                s.headers.update(dict(self.base_session.headers))
                s.cookies.update(self.base_session.cookies)
            self._local.session = s
        return s

    def _throttle(self) -> None:
        """令牌桶：拿到一个令牌才放行，否则睡到令牌够用为止。线程安全。"""
        while True:
            with self._rate_lock:
                now = time.monotonic()
                self._tokens = min(self._burst, self._tokens + (now - self._last_refill) * self._rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        retries: int = 3,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET JSON，带限速、重试与风控识别。线程安全。

        返回解析后的 JSON（结构由微博接口决定，故为 ``Any``）。
        风控与登录态失效直接抛异常、不重试——重试也只会继续被拦。
        """
        last_err: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            try:
                r = self._session().get(
                    url, params=params, timeout=15, allow_redirects=False, headers=headers
                )
                with self._count_lock:
                    self.request_count += 1

                if r.status_code in (302, 401, 414):
                    raise NotLoggedInError("登录态失效，请重新登录。")
                if r.status_code == 418 or r.status_code == 403:
                    raise RiskControlError("触发微博风控，请降低速度或稍后再试。")
                if r.status_code != 200:
                    last_err = RuntimeError(f"HTTP {r.status_code}")
                    time.sleep(min(2**attempt, 4))
                    continue

                data = r.json()
                # 风控统一返回 ok=-100
                if isinstance(data, dict) and data.get("ok") == -100:
                    raise RiskControlError("触发微博风控(ok=-100)，请降低速度。")
                return data
            except (requests.RequestException, ValueError) as e:
                last_err = e
                time.sleep(min(2**attempt, 4))
        if last_err is None:
            raise RuntimeError(f"请求未执行：{url}")
        raise last_err

    # ---------- 用户解析 ----------

    def resolve_user(self, user_input: str) -> UserInfo:
        """把 uid / 主页链接 / 昵称解析为 {'uid': str, 'screen_name': str}。"""
        s = user_input.strip()

        # 纯数字 uid
        if re.fullmatch(r"\d+", s):
            uid = s
        else:
            # weibo.com/u/123456
            m = re.search(r"weibo\.com/u/(\d+)", s)
            if m:
                uid = m.group(1)
            else:
                # weibo.com/n/昵称 → 跟随重定向拿到 uid
                m = re.search(r"weibo\.com/n/([^/\s?]+)", s)
                # 都不是的话，直接把输入当昵称试
                uid = self._resolve_by_nickname(m.group(1) if m else s)

        info = self.get_json("https://weibo.com/ajax/profile/info", {"uid": uid})
        user = (info.get("data") or {}).get("user") or {}
        if not user or not user.get("id"):
            raise UserResolveError(f"无法获取用户 {user_input} 的信息，请检查 uid 或链接是否正确。")
        return {
            "uid": str(user["id"]),
            "screen_name": user.get("screen_name") or "",
        }

    def _resolve_by_nickname(self, nickname: str) -> str:
        """通过移动端用户搜索接口解析昵称 → uid。

        weibo.com/n/{昵称} 不再 302 到 /u/{uid}（页面 JS 渲染），改用
        m.weibo.cn 的用户搜索容器接口，优先精确匹配昵称。
        """
        try:
            data = self.get_json(
                "https://m.weibo.cn/api/container/getIndex",
                {"containerid": f"100103type=1&q={requests.utils.quote(nickname)}"},
                headers={"Referer": "https://m.weibo.cn/"},
            )
            cards = ((data.get("data") or {}).get("cards")) or []
            # 收集候选用户
            candidates: list[dict[str, Any]] = []
            for c in cards:
                group = c.get("card_group") or []
                for x in group:
                    u = x.get("user")
                    if u and u.get("id"):
                        candidates.append(u)
                u = c.get("user")
                if u and u.get("id"):
                    candidates.append(u)
            # 精确匹配昵称
            for u in candidates:
                if u.get("screen_name") == nickname:
                    return str(u["id"])
            # 未精确匹配时取第一个候选
            if candidates:
                return str(candidates[0]["id"])
        except Exception:
            pass
        raise UserResolveError(
            f"无法从昵称「{nickname}」解析出 uid。请改用微博主页链接（weibo.com/u/xxx）或纯数字 uid。"
        )

    # ---------- 数据获取 ----------

    def fetch_user_weibos(
        self,
        uid: str,
        start_ts: float,
        end_ts: float,
        max_pages: int = 200,
        stop_seen: set[str] | None = None,
        pause_event: threading.Event | None = None,
        stop_event: threading.Event | None = None,
    ) -> list[WeiboItem]:
        """抓取某用户时间范围内的微博。

        注意：mymblog 接口第 1 页会混入「置顶微博」和极老的微博
        （排序被打乱），所以不能遇到早于 start_ts 就提前返回；
        改为全量翻页收集，最后统一过滤；从第 2 页起（倒序），
        整页都早于 start_ts 时提前停止。

        stop_seen：已扫描过的 mid 集合（增量刷新用）。从第 2 页起，
        若整页微博的 mid 都已扫描过，说明后续不会再有新微博，提前停止。
        pause_event：threading.Event，为 None 或已 set 时正常抓取；
        clear 时阻塞（暂停），set 后继续。
        stop_event：threading.Event，set 后尽快停止翻页。
        """
        results: list[WeiboItem] = []
        for page in range(1, max_pages + 1):
            if stop_event is not None and stop_event.is_set():
                break
            if pause_event is not None:
                pause_event.wait()  # 暂停点：翻页前
            # 列表翻页遇风控：等待后重试当前页，而不是中断整个流程
            while True:
                try:
                    data = self.get_json(
                        "https://weibo.com/ajax/statuses/mymblog",
                        {"uid": uid, "page": page, "feature": 0},
                    )
                    break
                except RiskControlError:
                    print(f"\r[风控] 微博列表第 {page} 页被限流，等待 150 秒后重试……    ", flush=True)
                    time.sleep(150)
            lst = ((data.get("data") or {}).get("list")) or []
            if not lst:
                break
            # 第 2 页起为严格时间倒序：整页都早于范围则后续只会更早
            if page >= 2:
                ts_list = [parse_weibo_time(it.get("created_at")) for it in lst]
                if all(t is not None and t < start_ts for t in ts_list):
                    break
                # 增量刷新：整页都已是扫过的微博 → 后面不会有新微博
                if stop_seen:
                    mids = {str(it.get("idstr") or "") for it in lst}
                    if mids and mids.issubset(stop_seen):
                        break
            for item in lst:
                wb = parse_weibo_item(item)
                if wb["created_at"] is None:
                    continue
                if wb["created_at"] < start_ts or wb["created_at"] > end_ts:
                    continue
                results.append(wb)
            # 翻页间隔由 get_json 内的 _throttle() 统一控制，
            # 此处不再重复 sleep（早期版本在此又等了一次 min_interval，等于双重限速）
        return results

    def fetch_reposts(self, mid: str, max_pages: int = 30, target_uid: str | None = None) -> list[RepostItem]:
        """某条微博的转发列表（含转发用户信息），按时间倒序。

        target_uid：若给出，则在**当前页内**找齐该用户的全部转发后立即返回，
        不再继续翻后续页——调用方只关心「目标用户是否转发过」，无需拉全量。
        返回的列表在命中后可能是截断的，请勿当作完整转发列表使用。

        说明：早期版本签名里的 start_ts 参数从未被函数体使用（死参数），
        且微博创建时间必然 >= 扫描窗口起点，转发时间又必然 >= 微博创建时间，
        因此按时间剪枝在本场景下永不触发。真正省请求的是「命中即停」。
        """
        results: list[RepostItem] = []
        for page in range(1, max_pages + 1):
            data = self.get_json(
                "https://weibo.com/ajax/statuses/repostTimeline",
                {"id": mid, "page": page, "moduleID": "feed", "type": 0},
            )
            lst = data.get("data") or []
            if isinstance(lst, dict):
                lst = lst.get("list") or []
            if not lst:
                break
            hit = False
            for item in lst:
                u = item.get("user") or {}
                if not u.get("id"):
                    continue
                uid = str(u["id"])
                results.append(
                    {
                        "uid": uid,
                        "screen_name": u.get("screen_name") or "",
                        "created_at": parse_weibo_time(item.get("created_at")),
                        "text": item.get("text_raw") or item.get("text") or "",
                        "mid": item.get("idstr") or "",
                    }
                )
                if target_uid and uid == str(target_uid):
                    hit = True
            # 本页已找到目标用户：同一页内可能有多条（重复转发），已全部收集，可以停
            if hit:
                return results
            if len(lst) < 20:
                break
        return results

    def fetch_comments(
        self, mid: str, page: int = 1, cid: str | None = None, flow: str = "1", bulletin: int = 0
    ) -> tuple[list[CommentItem], bool, str, int]:
        """某条微博的评论（cid=None）或某条评论的楼中楼回复（cid=评论id）。

        flow: "1"=按时间 "0"=按热度；bulletin=0 能拿到更多（含部分折叠）评论。
        返回 (out, has_more, tip_msg, total_number)
        total_number：接口声明的评论总数（缺失时为 0）。
        调用方可用它判断「本次是否已把接口认识的评论全部取回」。
        """
        params: dict[str, Any] = {
            "is_reload": 1,
            "id": mid,
            "is_show_bulletin": bulletin,
            "is_mix": 0,
            "count": config.COMMENT_PAGE_SIZE,
            "page": page,
            "flow": flow,
        }
        if cid:
            params["cid"] = cid
        data = self.get_json("https://weibo.com/ajax/statuses/buildComments", params)
        lst = data.get("data") or []
        if isinstance(lst, dict):
            lst = lst.get("list") or []
        out: list[CommentItem] = []
        for item in lst:
            u = item.get("user") or {}
            if not u.get("id"):
                continue
            rc = item.get("reply_comment") or {}
            # 内嵌楼中楼摘要（可能不全，但可用于判断该评论是否存在回复）
            inner: list[InnerComment] = []
            for ic in item.get("comments") or []:
                iu = ic.get("user") or {}
                if iu.get("id"):
                    inner.append(
                        {
                            "uid": str(iu["id"]),
                            "screen_name": iu.get("screen_name") or "",
                            "created_at": parse_weibo_time(ic.get("created_at")),
                            "text": ic.get("text_raw") or ic.get("text") or "",
                        }
                    )
            out.append(
                {
                    "uid": str(u["id"]),
                    "screen_name": u.get("screen_name") or "",
                    "created_at": parse_weibo_time(item.get("created_at")),
                    "text": item.get("text_raw") or item.get("text") or "",
                    "cid": str(item.get("id") or ""),
                    "reply_to": rc.get("text_raw") or rc.get("text") or "",
                    "reply_to_user": (rc.get("user") or {}).get("screen_name") or "",
                    "is_reply": bool(item.get("reply_comment"))
                    or (item.get("text_raw") or item.get("text") or "").lstrip().startswith("回复@"),
                    "inner": inner,
                }
            )
        total = data.get("total_number") or 0
        has_more = (
            (total > page * config.COMMENT_PAGE_SIZE) if total else (len(lst) >= config.COMMENT_PAGE_SIZE)
        )
        tip = data.get("tip_msg") or ""
        return out, has_more, tip, total

    def get_self_info(self) -> UserInfo:
        """当前登录账号的信息：{"uid": str, "screen_name": str}。

        「用户A 固定为扫码登录账号」的版本用它来填 A。
        /ajax/config/user 一次返回 uid 与 user.screen_name，无需额外请求。
        失败时返回 {"uid": "", "screen_name": ""}（调用方需自行判空）。
        """
        try:
            data = self.get_json("https://weibo.com/ajax/config/user")
            d = data.get("data") or {}
            uid = str(d.get("uid") or "")
            name = (d.get("user") or {}).get("screen_name") or ""
            # 少数账号在 config/user 里拿不到昵称，退回 profile/info 补一次
            if uid and not name:
                try:
                    info = self.get_json("https://weibo.com/ajax/profile/info", {"uid": uid})
                    name = ((info.get("data") or {}).get("user") or {}).get("screen_name") or ""
                except Exception:
                    pass
            return {"uid": uid, "screen_name": name}
        except Exception:
            return {"uid": "", "screen_name": ""}

    def get_self_uid(self) -> str:
        """当前登录账号的 uid（评论精选功能判断「自己的微博」用）。"""
        return self.get_self_info()["uid"]

    def fetch_approval_comments(self, mid: str, max_id: int = 0, count: int = 20) -> list[CommentItem]:
        """某条微博的「待审核评论」（博主评论精选功能，先审后发）。

        仅博主本人登录态可用；返回结构与 fetch_comments 的单条一致，
        并在每条上带 approval=True 标记。
        """
        out: list[CommentItem] = []
        page_max_id = max_id
        for _ in range(10):
            data = self.get_json(
                "https://weibo.com/ajax/approval/list",
                params={"id": mid, "max_id": page_max_id, "count": count, "filter_type": 0, "is_reload": 1},
            )
            d = data.get("data") or {}
            cm = d.get("comments") or []
            for item in cm:
                u = item.get("user") or {}
                if not u.get("id"):
                    continue
                out.append(
                    {
                        "uid": str(u["id"]),
                        "screen_name": u.get("screen_name") or "",
                        "created_at": parse_weibo_time(item.get("created_at")),
                        "text": item.get("text_raw") or item.get("text") or "",
                        "cid": str(item.get("id") or ""),
                        "reply_to": "",
                        "reply_to_user": "",
                        "is_reply": False,
                        "inner": [],
                        "approval": True,
                    }
                )
            next_max = d.get("max_id")
            total = d.get("total_number") or 0
            if not next_max or page_max_id == next_max or len(out) >= total:
                break
            page_max_id = next_max
        return out

    def fetch_attitudes(
        self, mid: str, max_pages: int = 18, target_uid: str | None = None
    ) -> list[AttitudeItem]:
        """某条微博的点赞用户列表（通过移动端接口，尽力而为）。

        网页版已无点赞列表接口；移动端 m.weibo.cn/api/attitudes/show
        每页 50 条。接口不稳定时返回空并标记不可用（由调用方判断）。

        target_uid：若给出，则在**当前页内**找齐该用户后立即返回，不再翻后续页。
        点赞数动辄上千（最多 18 页 × 50），而调用方只关心目标用户是否点过赞，
        命中即停可省掉绝大部分翻页。返回列表在命中后可能被截断。

        说明：早期版本签名里的 start_ts 参数从未被函数体使用（死参数），
        已移除；点赞列表同样不存在「早于扫描窗口」的条目可剪。
        """
        results: list[AttitudeItem] = []
        headers = {"Referer": "https://m.weibo.cn/"}
        for page in range(1, max_pages + 1):
            try:
                data = self.get_json(
                    "https://m.weibo.cn/api/attitudes/show",
                    {"id": mid, "page": page},
                    headers=headers,
                )
            except RiskControlError:
                raise
            except Exception:
                # 点赞接口不可用/无数据
                break
            if data.get("ok") != 1:
                break
            items = ((data.get("data") or {}).get("data")) or []
            if not items:
                break
            hit = False
            for item in items:
                u = item.get("user") or {}
                if not u.get("id"):
                    continue
                uid = str(u["id"])
                results.append(
                    {
                        "uid": uid,
                        "screen_name": u.get("screen_name") or "",
                        "created_at": parse_weibo_time(item.get("created_at")),
                    }
                )
                if target_uid and uid == str(target_uid):
                    hit = True
            if hit:
                return results
            if len(items) < 50:
                break
        return results
