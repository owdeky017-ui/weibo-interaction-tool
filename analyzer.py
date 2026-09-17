"""互动识别与聚合：扫描两人微博，找出双向转发/评论/楼中楼回复/@提及/点赞。

支持断点续传：断点记录已扫描微博 mid 与已发现的记录，
进程中断后可从上次位置继续，不重复扫描。
断点存储见 checkpoint.CheckpointStore（SQLite，增量写入）。

线程模型
--------
``_scan_weibo`` 把「转发 / 评论 / 点赞 / 待审评论」四组彼此独立的请求交给
``_run_tasks`` 并发执行。关键约定：**worker 线程只做网络 I/O，不写任何共享状态**
（``records`` / ``stats`` / 点赞状态机全部回到主线程处理），因此不存在竞态；
``RiskControlError`` 会被收集起来，在**调用线程**统一上抛，
以保持与串行版本一致的「由外层长等待后重试」语义。
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar, cast

from checkpoint import CheckpointStore
from client import RiskControlError, WeiboClient
from fetchers import extract_mentions, mentions_target
from models import CommentItem, FailedWeibo, InteractionRecord, UserInfo, WeiboItem
from utils import fmt_cst

T = TypeVar("T")

_REPLY_PAT = re.compile(r"^回复@([^:：\s]+)")


def _reply_target_name(text: str | None) -> str:
    """从楼中楼回复文本提取被回复人昵称，如「回复@owdeky:厨师正在备餐」→ owdeky。"""
    m = _REPLY_PAT.match((text or "").lstrip())
    return m.group(1) if m else ""


class InteractionAnalyzer:
    """扫描两个用户的微博，聚合出双向互动记录。"""

    def __init__(
        self,
        client: WeiboClient,
        user_a: UserInfo,
        user_b: UserInfo,
        types: set[str],
        deep_replies: bool = True,
        speed: int = 2,
        progress_cb: Callable[[str], None] | None = None,
        checkpoint: str | None = None,
        login_uid: str = "",
    ) -> None:
        self.client = client
        # 并发度取自 client（由 config.SPEED_WORKERS 按速度档决定）；1 = 完全串行
        self.workers = max(1, int(getattr(client, "workers", 1) or 1))
        self.a = user_a  # {"uid":.., "screen_name":..}
        self.b = user_b
        self.types = types  # {"转发","评论","@提及","点赞"}
        self.deep_replies = deep_replies
        self.login_uid = str(login_uid or "")  # 登录账号 uid（待审评论仅博主自己可见）
        self.progress_cb: Callable[[str], None] = progress_cb or (lambda msg: None)
        self.checkpoint_path = checkpoint
        self.store = CheckpointStore(checkpoint) if checkpoint else None
        # 并发扫描时多个线程会同时改 records / _seen_keys / stats，用锁保护
        self._lock = threading.RLock()
        self.records: list[InteractionRecord] = []
        self.scanned_mids: set[str] = set()
        self.last_run_ts = 0.0  # 上次扫描完成时刻（增量窗口基准）
        self._seen_keys: set[tuple[str, ...]] = set()  # 已记录的唯一键（去重，防重扫产生重复）
        self.like_api_ok = True
        self.like_fail_streak = 0
        self.like_disabled_at = -(10**9)  # 点赞接口被暂停时的扫描计数（周期重试用）
        self.scan_count = 0  # 累计扫描微博数
        self.failed_weibos: list[FailedWeibo] = []  # 单条微博抓取最终失败列表（导出时提示）
        self.stats: dict[str, Any] = {"A微博数": 0, "B微博数": 0, "请求数": 0}
        # 由 run() 注入；为 None 表示「不暂停 / 不中止」。
        # 原先这两个属性只在 run() 里赋值，导致 _scan_weibo/_scan_comments
        # 无法脱离 run() 单独调用（例如写单元测试时会 AttributeError）。
        self.pause_event: threading.Event | None = None
        self.stop_event: threading.Event | None = None
        self._load_checkpoint()

    def _log(self, msg: str) -> None:
        self.progress_cb(msg)

    # ---------- 断点续传 ----------

    def _load_checkpoint(self) -> None:
        if not self.store:
            return
        try:
            cp = self.store.load()
            self.records = cast("list[InteractionRecord]", cp["records"])
            self.scanned_mids = set(cp["scanned_mids"])
            self.last_run_ts = cp["last_run_ts"] or 0.0
            # 从已存记录重建去重键，保证重扫窗口内旧微博不重复记录
            for r in self.records:
                k = r.get("_key")
                if k:
                    self._seen_keys.add(tuple(k))
            self.stats.update(cp["stats"])
            if cp["like_api_ok"] is not None:
                self.like_api_ok = cp["like_api_ok"]
            for f in cp["failed_weibos"]:
                if f not in self.failed_weibos:
                    self.failed_weibos.append(cast("FailedWeibo", f))
            if cp.get("migrated_count"):
                self._log(
                    f"[续传] 已把旧 JSON 断点里的 {cp['migrated_count']} 条记录"
                    f"迁移到 SQLite（原文件保留未动）。"
                )
            if self.records or self.scanned_mids:
                self._log(
                    f"[续传] 已恢复 {len(self.records)} 条记录，已扫 {len(self.scanned_mids)} 条微博"
                    + (f"，失败 {len(self.failed_weibos)} 条" if self.failed_weibos else "")
                )
        except Exception as e:
            self._log(f"[警告] checkpoint 读取失败({e})，从零开始。")

    def _save_checkpoint(self) -> None:
        if not self.store:
            return
        try:
            self.store.save(
                cast("list[dict[str, Any]]", self.records),
                self.scanned_mids,
                time.time(),
                self.stats,
                self.like_api_ok,
                cast("list[dict[str, Any]]", self.failed_weibos),
            )
        except Exception as e:
            # 早期版本这里是静默的 `except Exception: pass`：断点写失败时用户毫无感知，
            # 以为存了其实没存，下次「继续」会从更早的位置重扫甚至丢进度。
            # 改为显式提示（progress_cb 自身异常不再向外抛，避免掩盖原始错误）。
            with contextlib.suppress(Exception):
                self._log(f"[警告] 断点保存失败（{type(e).__name__}: {e}），本次进度可能无法续传。")

    # ---------- 主入口 ----------

    def run(
        self,
        start_ts: float,
        end_ts: float,
        quick: bool = False,
        pause_event: threading.Event | None = None,
        stop_event: threading.Event | None = None,
    ) -> list[InteractionRecord]:
        """扫描分析两用户的互动。

        增量窗口：有历史扫描记录时，列表与分析覆盖「上次扫描时刻回退 3 天」
        起的全部微博——旧微博也可能被对方新评论/新回复/新点赞
        （如对方回复了你很久以前的微博），窗口内的已扫微博也重新扫描；
        3 天以外的旧微博跳过以控制耗时。首次运行无记录时从 start_ts 全量。

        quick=True（快速模式）：列表翻页遇到整页都已扫过即停，且跳过
          已扫微博，只扫新增微博。速度快，但会漏「旧微博被新互动」。
        pause_event：threading.Event；clear 时暂停扫描，set 后继续。
        stop_event：threading.Event；set 后尽快停止扫描，保留已扫到的记录。
        """
        self.pause_event = pause_event
        self.stop_event = stop_event
        scan_from = start_ts
        stop_seen: set[str] | None
        if quick:
            stop_seen = set(self.scanned_mids) if self.scanned_mids else None
        else:
            stop_seen = None
            if self.scanned_mids:
                last = self.last_run_ts or end_ts
                window = last - 3 * 86400
                if start_ts >= window:
                    # 用户范围落在增量窗口内 → 只重扫最近 3 天（旧微博的新互动）
                    scan_from = window
                else:
                    # 用户指定的开始时间早于增量窗口（如重新选 2025-11-01）→ 全量重扫
                    self._log(f"[增量] 检测到指定范围早于增量窗口（{fmt_cst(window)}），本次全量重扫。")
        self._log(f"[增量] 本次重扫窗口：{fmt_cst(scan_from)} ~ {fmt_cst(end_ts)}")
        self._log("正在抓取 A 的微博列表……")
        weibos_a = self.client.fetch_user_weibos(
            self.a["uid"],
            scan_from,
            end_ts,
            stop_seen=stop_seen,
            pause_event=pause_event,
            stop_event=stop_event,
        )
        self.stats["A微博数"] = len(weibos_a)
        self._log(f"[进度] A 命中 {len(weibos_a)} 条微博")
        if stop_event is not None and stop_event.is_set():
            self._log("[停止] 收到结束请求，跳过 B 的扫描（保留已扫到的记录）。")
            self._save_checkpoint()
            return self.records

        self._log("正在抓取 B 的微博列表……")
        weibos_b = self.client.fetch_user_weibos(
            self.b["uid"],
            scan_from,
            end_ts,
            stop_seen=stop_seen,
            pause_event=pause_event,
            stop_event=stop_event,
        )
        self.stats["B微博数"] = len(weibos_b)
        self._log(f"[进度] B 命中 {len(weibos_b)} 条微博")

        total = len(weibos_a) + len(weibos_b)
        # A→B 与 B→A 两段扫描逻辑完全一致，只是 owner/other 对调。
        # 早期版本是两段复制粘贴的代码（改一处容易漏另一处），提取为 _scan_batch。
        # done 跨两段累计，用于进度显示与每 20 条存一次断点。
        done = 0
        done = self._scan_batch(
            weibos_a, self.a, self.b, "A", "B", scan_from, quick, pause_event, stop_event, done, total
        )
        done = self._scan_batch(
            weibos_b, self.b, self.a, "B", "A", scan_from, quick, pause_event, stop_event, done, total
        )
        self._save_checkpoint()
        return self.records

    def _scan_batch(
        self,
        weibos: list[WeiboItem],
        owner: UserInfo,
        other: UserInfo,
        owner_label: str,
        other_label: str,
        scan_from: float,
        quick: bool,
        pause_event: threading.Event | None,
        stop_event: threading.Event | None,
        done: int,
        total: int,
    ) -> int:
        """扫描一批微博（A→B 与 B→A 共用）。

        done 是跨批次的累计进度，返回值供下一批次接着用。
        """
        for wb in weibos:
            if stop_event is not None and stop_event.is_set():
                self._log("[停止] 收到结束请求，停止扫描（保留已扫到的记录）……")
                break
            if pause_event is not None:
                pause_event.wait()  # 暂停点：每条微博扫描前
            done += 1
            if quick and wb["mid"] in self.scanned_mids:
                continue
            created = wb["created_at"]
            if created is not None and created < scan_from:
                continue
            self._log(f"[{owner_label}→{other_label} 扫描] {done}/{total}")
            self._scan_weibo(wb, owner=owner, other=other, owner_label=owner_label, other_label=other_label)
            self.scanned_mids.add(wb["mid"])
            if done % 20 == 0:
                self._save_checkpoint()
        return done

    # ---------- 单条微博扫描 ----------

    def _retry(
        self, fn: Callable[[], T], attempts: int = 3, base_delay: float = 3.0
    ) -> tuple[T | None, Exception | None]:
        """执行 fn()，网络类异常自动重试（最多 attempts 次）；最终失败返回 (None, err)。

        RiskControlError 一律上抛（由上层做长等待）。
        """
        last_err: Exception | None = None
        for i in range(attempts):
            if self.stop_event is not None and self.stop_event.is_set():
                return None, None  # 收到结束请求：不再重试，放弃本条
            try:
                return fn(), None
            except RiskControlError:
                raise
            except Exception as e:
                last_err = e
                if i < attempts - 1:
                    delay = base_delay * (i + 1)
                    self._log(
                        f"  [重试 {i + 1}/{attempts - 1}] {type(e).__name__}: {e}，{delay:.0f} 秒后重试……"
                    )
                    time.sleep(delay)
        return None, last_err

    def _record_fail(self, mid: str, wb: WeiboItem | None, stage: str, err: Exception) -> None:
        with self._lock:
            self.failed_weibos.append(
                {
                    "mid": mid,
                    "url": wb["url"] if wb else "",
                    "stage": stage,
                    "reason": f"{type(err).__name__}: {err}",
                }
            )
        self._log(f"  [失败] {stage}扫描 {mid} 重试后仍失败：{type(err).__name__}: {err}")

    def _scan_weibo(
        self, wb: WeiboItem, owner: UserInfo, other: UserInfo, owner_label: str, other_label: str
    ) -> None:
        mid = wb["mid"]
        if not mid:
            return
        if self.stop_event is not None and self.stop_event.is_set():
            return  # 收到结束请求：本条微博不再扫描，由外层循环统一收尾
        other_uid = other["uid"]
        other_name = other["screen_name"]
        with self._lock:
            self.scan_count += 1

        # 1) @ 提及（owner 的微博里 @ 了 other）—— 纯本地解析，不涉及网络
        if "@提及" in self.types:
            mentions = extract_mentions(wb["text_html"])
            if mentions_target(mentions, other_uid, other_name):
                self._add(owner_label, other_label, "@提及", wb, f"@{other_name}", None)

        # 2~5) 转发 / 评论 / 点赞 / 待审评论
        # 这四组请求彼此独立、且都只是读，交给线程池并发执行（I/O 密集，
        # 并发能把网络等待重叠掉；请求总数不变）。
        # 关键约定：**worker 线程只做网络 I/O，不写任何共享状态**——
        # records / stats / 点赞状态机全部回到主线程处理，因此不存在竞态。
        tasks: list[tuple[str, Callable[[], Any], bool]] = []
        if "转发" in self.types and wb["reposts_count"] > 0:
            # 命中对方即停，不翻完全部转发页
            tasks.append(("reposts", lambda: self.client.fetch_reposts(mid, target_uid=other_uid), True))
        if "评论" in self.types and wb["comments_count"] > 0:
            tasks.append(("comments", lambda: self._fetch_comments_all(mid), True))
        if "点赞" in self.types and wb["attitudes_count"] > 0:
            # 点赞接口被暂停时，每 50 条微博重试一次
            if not self.like_api_ok and self.scan_count - self.like_disabled_at >= 50:
                self.like_api_ok = True
                self.like_fail_streak = 0
                self._log("  [提示] 点赞接口已暂停一段时间，重新尝试……")
            if self.like_api_ok:
                tasks.append(("likes", lambda: self.client.fetch_attitudes(mid, target_uid=other_uid), False))
        # 待审核评论（博主「评论精选」先审后发，未放出的评论只有博主自己可见）
        # 仅当这条微博是登录账号自己发的、且勾选了评论时才检查
        if "评论" in self.types and self.login_uid and str(owner["uid"]) == str(self.login_uid):
            tasks.append(("approval", lambda: self.client.fetch_approval_comments(mid), True))

        results = self._run_tasks(tasks)

        # --- 转发 ---
        if "reposts" in results:
            reposts, err = results["reposts"]
            with self._lock:
                self.stats["请求数"] += 1
            if err:
                self._record_fail(mid, wb, "转发", err)
            else:
                for r in reposts:
                    if r["uid"] == other_uid:
                        self._add(owner_label, other_label, "转发", wb, r["text"], r)

        # --- 评论 + 楼中楼 ---
        if "comments" in results:
            payload, err = results["comments"]
            if err:
                with self._lock:
                    self.stats["请求数"] += 1
                self._record_fail(mid, wb, "评论", err)
            else:
                comments_all, reqs, tip = payload
                with self._lock:
                    self.stats["请求数"] += reqs
                    if tip:
                        self.stats.setdefault("拉黑提示", tip)
                self._process_comments(
                    wb, comments_all, other_uid, other_name, owner_label, other_label, owner["uid"]
                )

        # --- 点赞（尽力而为；连续失败后暂停，每 50 条微博自动重试一次） ---
        if "likes" in results:
            likers, err = results["likes"]
            with self._lock:
                self.stats["请求数"] += 1
            if err:
                self.like_fail_streak += 1
                if self.like_fail_streak >= 3:
                    self.like_api_ok = False
                    self.like_disabled_at = self.scan_count
                    self._log(f"  [提示] 点赞接口连续异常({err})，暂停点赞扫描（每 50 条微博自动重试）。")
            else:
                hit = any(liker["uid"] == other_uid for liker in likers)
                if hit:
                    self.like_fail_streak = 0
                    self._add(owner_label, other_label, "点赞", wb, "点赞了这条微博", None)
                elif not likers:
                    # 接口偶发空返回：连续 3 次才暂停
                    self.like_fail_streak += 1
                    if self.like_fail_streak >= 3:
                        self.like_api_ok = False
                        self.like_disabled_at = self.scan_count
                        self._log("  [提示] 点赞列表接口连续不可用，暂停点赞扫描（每 50 条微博自动重试）。")
                else:
                    self.like_fail_streak = 0

        # --- 待审核评论 ---
        if "approval" in results:
            ap, err = results["approval"]
            with self._lock:
                self.stats["请求数"] += 1
            if err:
                self._record_fail(mid, wb, "待审评论", err)
            else:
                with self._lock:
                    self.stats["待审评论数"] = self.stats.get("待审评论数", 0) + len(ap)
                for r in ap:
                    if r["uid"] == other_uid:
                        self._add(owner_label, other_label, "评论", wb, f"【待审核】{r['text']}", r)

    def _run_tasks(
        self, tasks: list[tuple[str, Callable[[], Any], bool]]
    ) -> dict[str, tuple[Any, Exception | None]]:
        """并发执行各组请求，返回 {name: (value, err)}。

        worker 线程只做网络 I/O，不触碰共享状态。
        RiskControlError 会被收集起来，在**调用线程**统一上抛，
        保持与串行版本一致的「由外层长等待后重试」行为。

        tasks 元素为 (name, fn, use_retry)：use_retry=True 时用 self._retry
        包一层（网络异常自动重试），False 时直接调用（点赞接口自行容错）。
        """
        if not tasks:
            return {}
        results: dict[str, tuple[Any, Exception | None]] = {}
        first_risk: RiskControlError | None = None

        def worker(
            name: str, fn: Callable[[], Any], use_retry: bool
        ) -> tuple[str, Any, Exception | None, RiskControlError | None]:
            try:
                if use_retry:
                    v, e = self._retry(fn)
                    return name, v, e, None
                return name, fn(), None, None
            except RiskControlError as e:
                return name, None, None, e
            except Exception as e:
                return name, None, e, None

        workers = max(1, min(self.workers, len(tasks)))
        outcomes: list[tuple[str, Any, Exception | None, RiskControlError | None]]
        if workers <= 1:
            outcomes = [worker(*t) for t in tasks]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                outcomes = [f.result() for f in [ex.submit(worker, *t) for t in tasks]]

        for name, val, err, risk in outcomes:
            results[name] = (val, err)
            if risk is not None and first_risk is None:
                first_risk = risk
        if first_risk is not None:
            raise first_risk
        return results

    def _fetch_comments_all(self, mid: str) -> tuple[list[CommentItem], int, str]:
        """纯网络：翻完某条微博的评论（热度/时间双排序按 cid 去重）。

        **不写任何共享状态**——请求数、拉黑提示都由调用方在主线程累加，
        因此可以安全地放进线程池并发调用。
        返回 (comments_all, requests_made, blacklist_tip)
        """
        seen_cids: set[str] = set()
        comments_all: list[CommentItem] = []
        requests_made = 0
        blacklist_tip = ""

        # 翻页策略：只要返回非空就继续，直到空页或达到最大页数（不依赖"不足一页就停"，
        # 因为微博接口每页返回数量不固定，19 条后面可能还有第 2 页）
        #
        # 相对早期版本的两处优化：
        #  1) 用上接口返回的 has_more：本页已到底就直接停，不再多空翻一页；
        #  2) 时间序（flow="1"）若已把接口声明的 total_number 条评论全部取回，
        #     说明两种排序看到的评论集合一致，此时跳过热度序（flow="0"）。
        #     双排序的意义只在于「两种排序能看到的评论不同」（折叠评论），
        #     集合已完全一致时再跑一遍纯属浪费。
        #     只有当时间序被页数上限截断、或拿到的条数少于 total 时才补跑热度序，
        #     因此不会降低原有覆盖度；接口未返回 total_number 时同样补跑，保持原行为。
        for flow in ("1", "0"):
            page = 1
            collected_here = 0
            total = 0
            complete = False
            while page <= 10:
                if self.stop_event is not None and self.stop_event.is_set():
                    break  # 收到结束请求：停止翻评论
                comments, has_more, tip, total = self.client.fetch_comments(mid, page=page, flow=flow)
                requests_made += 1
                if tip and "拉黑" in tip and not blacklist_tip:
                    blacklist_tip = tip
                if not comments:
                    complete = True  # 空页 = 已到末页
                    break
                for c in comments:
                    if c["cid"] in seen_cids:
                        continue
                    seen_cids.add(c["cid"])
                    comments_all.append(c)
                    collected_here += 1
                if not has_more:
                    complete = True  # 接口明确表示没有更多
                    break
                page += 1
            # 时间序已完整覆盖接口认识的全部评论 → 无需再跑热度序
            if flow == "1" and complete and total and collected_here >= total:
                break
        return comments_all, requests_made, blacklist_tip

    def _process_comments(
        self,
        wb: WeiboItem,
        comments_all: list[CommentItem],
        other_uid: str,
        other_name: str,
        owner_label: str,
        other_label: str,
        owner_uid: str | None = None,
    ) -> None:
        """把抓到的评论整理成互动记录（主线程调用，会写 records/stats）。"""
        for c in comments_all:
            # 停止只中断网络请求（翻页 break）；已抓到的评论照常整理保留
            # 网页版评论列表是「主评论 + 楼中楼回复(回复@xxx)」的扁平混合：
            # 有回复对象或文本以"回复@"开头 → 楼中楼回复，否则为主评论。
            itype = "评论回复" if c.get("is_reply") else "评论"
            if c["uid"] == other_uid:
                self._add(owner_label, other_label, itype, wb, c["text"], c)
            # 微博作者在评论区回复他人：被回复的是对方 → 记 owner→other 评论回复
            elif c["uid"] == owner_uid and c.get("is_reply") and _reply_target_name(c["text"]) == other_name:
                self._add(
                    owner_label,
                    other_label,
                    "评论回复",
                    wb,
                    c["text"],
                    c,
                    direction=f"{owner_label}→{other_label}",
                )
            # 内嵌楼中楼（部分接口会返回，如移动端）直接检查
            for ic in c.get("inner") or []:
                if ic["uid"] == other_uid:
                    self._add(owner_label, other_label, "评论回复", wb, ic["text"], ic)
                elif ic["uid"] == owner_uid and _reply_target_name(ic["text"]) == other_name:
                    self._add(
                        owner_label,
                        other_label,
                        "评论回复",
                        wb,
                        ic["text"],
                        ic,
                        direction=f"{owner_label}→{other_label}",
                    )
        # 记录可见评论数与总数差异（可能含被折叠/隐藏评论）
        with self._lock:
            self.stats["评论可见数"] = len(comments_all)

    def _scan_comments(
        self,
        wb: WeiboItem,
        other_uid: str,
        other_name: str,
        owner_label: str,
        other_label: str,
        owner_uid: str | None = None,
    ) -> None:
        """「抓 + 整理」的顺序版（保留原方法名，便于单独调用与测试）。"""
        comments_all, reqs, tip = self._fetch_comments_all(wb["mid"])
        with self._lock:
            self.stats["请求数"] += reqs
            if tip:
                self.stats.setdefault("拉黑提示", tip)
        self._process_comments(wb, comments_all, other_uid, other_name, owner_label, other_label, owner_uid)

    # ---------- 记录 ----------

    def _add(
        self,
        owner_label: str,
        other_label: str,
        itype: str,
        wb: WeiboItem,
        content: str | None,
        actor: Mapping[str, Any] | None,
        direction: str | None = None,
    ) -> None:
        # owner_label 是微博作者，actor(other) 是互动者。
        # 互动者转发/评论/点赞/回复了作者的微博 → 方向 = 互动者→作者；
        # 作者在微博中 @ 了对方 → 方向 = 作者→对方；
        # 作者在评论区回复对方评论（楼中楼）→ 方向 = 作者→对方（显式传入）。
        if direction is None:
            direction = f"{other_label}→{owner_label}"
        if itype == "@提及":
            direction = f"{owner_label}→{other_label}"
        owner_name = self.a["screen_name"] if owner_label == "A" else self.b["screen_name"]
        other_name = self.a["screen_name"] if other_label == "A" else self.b["screen_name"]

        actor_name = ""
        actor_url = ""
        actor_time = ""
        if actor:
            actor_name = actor.get("screen_name") or ""
            actor_url = f"https://weibo.com/u/{actor.get('uid', '')}" if actor.get("uid") else ""
            actor_time = fmt_cst(actor.get("created_at"))

        # 去重：同一互动（评论/回复/转发/点赞/@提及）只记一次，
        # 防止窗口内旧微博重扫产生重复记录
        key = self._record_key(itype, direction, wb, actor)
        with self._lock:
            if key is not None and key in self._seen_keys:
                return
            self.records.append(
                {
                    "时间": actor_time,
                    "互动类型": itype,
                    "方向": direction,
                    "发起方": actor_name or (f"@{other_name}" if itype == "@提及" else other_name),
                    "微博作者": owner_name,
                    "微博内容": (wb["text_plain"] or ""),
                    "互动内容": (content or ""),
                    "被回复人": (actor.get("reply_to_user") if actor and itype == "评论回复" else "") or "",
                    "被回复评论": (actor.get("reply_to") if actor and itype == "评论回复" else "") or "",
                    "微博时间": fmt_cst(wb["created_at"]),
                    "微博链接": wb["url"],
                    "互动链接": actor_url,
                    "_key": list(key) if key is not None else None,
                }
            )
            if key is not None:
                self._seen_keys.add(key)

    @staticmethod
    def _record_key(
        itype: str, direction: str, wb: WeiboItem, actor: Mapping[str, Any] | None
    ) -> tuple[str, ...] | None:
        """互动唯一键：评论/回复用 cid，转发用转发微博 mid，
        点赞用 微博mid+uid，@提及用 微博mid。"""
        mid = wb.get("mid") or wb.get("url") or ""
        if itype in ("评论", "评论回复"):
            cid = (actor or {}).get("cid") or ""
            return ("互动", direction, cid) if cid else None
        if itype == "转发":
            rid = (actor or {}).get("mid") or ""
            return ("互动", direction, rid) if rid else None
        if itype == "点赞":
            uid = (actor or {}).get("uid") or ""
            return ("点赞", mid, uid) if uid and mid else None
        if itype == "@提及":
            return ("互动", direction, mid)
        return None
