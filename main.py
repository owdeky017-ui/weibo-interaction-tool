"""主程序：提取两个微博用户之间的互动记录。

A_MODE 是编译期常量（见 build_mode.py），本版取 "self"：
**用户A 固定为扫码登录的账号**，不可手填；只需指定用户B。

用法：
  python main.py --u2 用户B [--days 30] [--speed 2] ...
  python main.py   # 交互式引导（只询问用户B）

用户标识支持：纯数字 uid / weibo.com/u/xxx 链接 / 昵称。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence

try:
    # TextIOWrapper 才有 reconfigure；stdout 被重定向成别的对象时会缺这个属性
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:
    pass

import config
import login as login_mod
from analyzer import InteractionAnalyzer
from client import NotLoggedInError, RiskControlError, WeiboClient
from exporter import export
from models import InteractionRecord, UserInfo
from utils import fmt_cst, parse_date_input

try:
    from build_mode import A_MODE
except Exception:
    A_MODE = "self"

# 用户A 是否固定为扫码登录的账号
SELF_IS_A = A_MODE == "self"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    desc = "提取当前登录账号与另一个微博用户的互动记录" if SELF_IS_A else "提取微博两个用户的互动记录"
    p = argparse.ArgumentParser(description=desc)
    if SELF_IS_A:
        p.add_argument("--u1", help=argparse.SUPPRESS)  # 已废弃，见 main() 中的提示
    else:
        p.add_argument("--u1", help="用户A：uid / 主页链接 / 昵称")
    p.add_argument(
        "--u2",
        help=(
            "用户B：uid / 主页链接 / 昵称（用户A 固定为登录账号）"
            if SELF_IS_A
            else "用户B：uid / 主页链接 / 昵称"
        ),
    )
    p.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"抓取最近 N 天（默认 {config.DEFAULT_DAYS}，与 --start 二选一）",
    )
    p.add_argument("--start", help="起始日期 YYYY-MM-DD（东八区）")
    p.add_argument("--end", help="结束日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--types", default="转发,评论,@提及,点赞", help="互动类型，逗号分隔：转发,评论,@提及,点赞")
    p.add_argument(
        "--no-replies", action="store_true", help="不深度扫描评论楼中楼（更快但会漏掉评论下的回复互动）"
    )
    p.add_argument(
        "--speed", type=int, choices=[1, 2, 3], default=2, help="抓取速度：1慢(稳) 2中 3快(易风控)"
    )
    p.add_argument("--cookie", help="手动粘贴浏览器 Cookie 字符串（替代扫码）")
    p.add_argument("--out", help="输出目录（默认 data/output）")
    p.add_argument(
        "--resume", action="store_true", help="断点续传：从上次 checkpoint 继续扫描（中断后重跑用）"
    )
    p.add_argument("--checkpoint", default=None, help="checkpoint 路径（默认 data/checkpoint.db，SQLite）")
    p.add_argument("--wait", type=int, default=180, help="触发风控后的等待秒数，之后自动继续（默认 180）")
    return p.parse_args(argv)


def interactive_ask(args: argparse.Namespace) -> argparse.Namespace:
    """无参数时交互式收集配置。"""
    if SELF_IS_A:
        if not args.u2:
            args.u2 = input("请输入用户B（uid / 主页链接 / 昵称）：").strip()
    else:
        if not args.u1:
            args.u1 = input("请输入用户A（uid / 主页链接 / 昵称）：").strip()
        if not args.u2:
            args.u2 = input("请输入用户B（uid / 主页链接 / 昵称）：").strip()
    if not args.start and not args.days:
        days = input(f"抓取最近多少天（默认 {config.DEFAULT_DAYS}）：").strip()
        if days.isdigit():
            args.days = int(days)
    return args


def compute_range(args: argparse.Namespace) -> tuple[float, float]:
    """计算抓取时间范围（UTC 时间戳）。"""
    from datetime import datetime, timedelta

    from utils import CST

    now = datetime.now(CST)
    end_ts = parse_date_input(args.end) if args.end else now.timestamp()
    if args.start:
        start_ts = parse_date_input(args.start)
    else:
        days = args.days if args.days else config.DEFAULT_DAYS
        start_ts = (now - timedelta(days=days)).timestamp()
    if start_ts >= end_ts:
        raise SystemExit("错误：起始时间必须早于结束时间。")
    return start_ts, end_ts


TYPE_ALIAS: dict[str, str] = {
    "转发": "转发",
    "repost": "转发",
    "reposts": "转发",
    "forward": "转发",
    "评论": "评论",
    "comment": "评论",
    "comments": "评论",
    "点赞": "点赞",
    "like": "点赞",
    "likes": "点赞",
    "attitude": "点赞",
    "@提及": "@提及",
    "mention": "@提及",
    "at": "@提及",
    "@": "@提及",
}


def normalize_types(raw: str) -> set[str]:
    out = set()
    for t in raw.replace("，", ",").split(","):
        t = t.strip().lower()
        if not t:
            continue
        mapped = TYPE_ALIAS.get(t) or TYPE_ALIAS.get(t.strip("@"))
        if mapped:
            out.add(mapped)
    return out


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if SELF_IS_A:
        if args.u1:
            raise SystemExit(
                "错误：本版本「用户A」固定为当前扫码登录的账号，不再支持 --u1。\n"
                "      只需用 --u2 指定对方账号即可，例如：\n"
                "      python main.py --u2 1234567890 --days 30"
            )
    else:
        if not args.u1:
            raise SystemExit(
                "错误：缺少用户A。请用 --u1 指定，例如：\n"
                "      python main.py --u1 1234567890 --u2 9876543210 --days 30\n"
                "      （不带参数运行会进入交互式引导，逐步询问）"
            )
    args = interactive_ask(args)
    start_ts, end_ts = compute_range(args)
    types = normalize_types(args.types)
    if not types:
        raise SystemExit(
            "错误：--types 为空或不识别。可用：转发/评论/@提及/点赞（或 repost/comment/mention/like）"
        )

    print(f"时间范围：{fmt_cst(start_ts)} ~ {fmt_cst(end_ts)}（东八区）")
    speed_label = {1: "慢", 2: "中", 3: "快"}.get(args.speed, "中")
    print(f"互动类型：{'、'.join(sorted(types))} | 速度：{speed_label}")

    # ---------- 登录 ----------
    session = login_mod.login(cookie_str=args.cookie)
    login_mod.save_cookies(session)
    client = WeiboClient(session, speed=args.speed)

    # ---------- 解析用户 ----------
    print("正在解析用户信息……")
    if SELF_IS_A:
        # 用户A 固定 = 当前登录账号（本版本不支持手填）
        login_info = client.get_self_info()
        login_uid = login_info["uid"]
        if not login_uid:
            raise SystemExit("错误：无法获取当前登录账号的 uid（登录态可能已失效），请重新登录后再试。")
        try:
            user_a = client.resolve_user(login_uid)
        except Exception:
            # 拿不到昵称不影响抓取（uid 才是关键），用占位名兜底
            user_a = UserInfo(uid=login_uid, screen_name=login_info["screen_name"] or "我")
        user_b = client.resolve_user(args.u2)
        if user_a["uid"] == user_b["uid"]:
            raise SystemExit("错误：用户B 不能是当前登录账号本人（查自己与自己的互动没有意义）。")
        print(f"用户A：{user_a['screen_name']} (uid {user_a['uid']})  ← 当前登录账号")
    else:
        user_a = client.resolve_user(args.u1)
        user_b = client.resolve_user(args.u2)
        if user_a["uid"] == user_b["uid"]:
            raise SystemExit("错误：用户A 和用户B 不能是同一个。")
        login_uid = client.get_self_uid()
        print(f"用户A：{user_a['screen_name']} (uid {user_a['uid']})")
    print(f"用户B：{user_b['screen_name']} (uid {user_b['uid']})")

    # ---------- 分析互动（风控自动等待重试 + 断点续传） ----------
    if login_uid:
        print(f"登录账号 uid：{login_uid}（将同时检查自己微博下的待审核评论）")

    def progress(msg: str) -> None:
        print(f"\r{msg}    ", end="", flush=True)
        if msg.startswith("["):
            print()

    checkpoint = args.checkpoint or os.path.join(config.DATA_DIR, "checkpoint.db")
    if args.resume and os.path.exists(checkpoint):
        print(f"[续传] 使用 checkpoint：{checkpoint}")
    elif args.resume:
        print("[续传] 未找到 checkpoint，将从零开始。")

    print("\n开始扫描互动（微博数量多时请耐心等待，速度过快会触发风控）……")
    records: list[InteractionRecord] = []
    while True:
        try:
            analyzer = InteractionAnalyzer(
                client,
                user_a,
                user_b,
                types=types,
                deep_replies=not args.no_replies,
                speed=args.speed,
                progress_cb=progress,
                checkpoint=checkpoint,
                login_uid=login_uid,
            )
            records = analyzer.run(start_ts, end_ts)
            break
        except NotLoggedInError:
            print(
                "\n[错误] 登录态已失效。请删除 data/cookies.json 后重新运行，或使用 --cookie 粘贴新 cookie。"
            )
            return
        except RiskControlError as e:
            print(f"\n[风控] {e}。等待 {args.wait} 秒后自动继续（进度已存入 checkpoint）……")
            time.sleep(args.wait)
    print()

    # ---------- 导出 ----------
    result = export(
        records, user_a, user_b, out_dir=args.out, failed_weibos=analyzer.failed_weibos, enable_refresh=True
    )
    print(f"\n发现互动记录 {result['count']} 条")
    pivot = result.get("pivot")
    if pivot and pivot.get("rows"):
        print("\n汇总（类型 × 方向）：")
        cols = pivot["columns"]
        widths = [max(len(str(cols[i])), *(len(str(r[i])) for r in pivot["rows"])) for i in range(len(cols))]
        for line in [cols] + pivot["rows"]:
            print("  " + "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(line)))
    print(f"\n已导出：\n  Excel: {result['excel']}\n  CSV:   {result['csv']}\n  HTML日志: {result['html']}")
    if not analyzer.like_api_ok:
        print("提示：点赞列表接口本次中途不可用（已自动暂停并周期重试），点赞部分可能不完整。")
    if analyzer.failed_weibos:
        print(
            f"提示：有 {len(analyzer.failed_weibos)} 条微博抓取失败（已重试 3 次），"
            f"其互动可能缺失，详见 HTML 顶部。"
        )
    if analyzer.stats.get("拉黑提示"):
        print(
            f"提示：评论区检测到「{analyzer.stats['拉黑提示']}」——被博主拉黑的用户，"
            f"其评论对公众完全隐藏，公开接口无法获取。"
        )
    if analyzer.stats.get("评论可见数") is not None:
        print(
            f"提示：共扫描到可见评论 {analyzer.stats['评论可见数']} 条；"
            f"若评论区存在折叠/拉黑隐藏，实际评论数可能更多。"
        )
    if analyzer.stats.get("待审评论数"):
        print(
            f"提示：发现待审核评论 {analyzer.stats['待审评论数']} 条（含对方的，已【待审核】标注）；"
            f"通过审核后刷新可转为正式评论。"
        )


if __name__ == "__main__":
    main()
