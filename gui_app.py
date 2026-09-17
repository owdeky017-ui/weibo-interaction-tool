"""微博互动查询器（图形界面版，可打包分发给他人使用）。

流程：扫码登录微博 → 填写要对比的用户 → 选择范围/类型/速度 →
开始抓取 → 导出 Excel/CSV/HTML 日志并自动打开。

A_MODE 是编译期常量（见 build_mode.py），本版取 "self"：
用户A 固定为当前扫码登录的账号，只需填用户B。

线程模型
--------
tkinter 只能在主线程操作。所有后台线程（登录、抓取）一律通过 ``self.msg_q``
投递消息，由主线程的 ``_poll_queue``（``root.after(150, ...)``）消费并更新界面。
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timedelta
from functools import partial
from typing import Any

with contextlib.suppress(Exception):
    # TextIOWrapper 才有 reconfigure；stdout 被重定向成别的对象时会缺这个属性
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

import tkinter as tk
from tkinter import messagebox, ttk

import requests

import config
import login as login_mod
from analyzer import InteractionAnalyzer
from client import NotLoggedInError, RiskControlError, WeiboClient
from exporter import export
from models import InteractionRecord, UserInfo
from utils import CST, parse_date_input

try:
    from build_mode import A_MODE
except Exception:
    A_MODE = "self"

# 用户A 是否固定为扫码登录的账号
SELF_IS_A = A_MODE == "self"

try:
    from PIL import Image, ImageTk

    HAS_PIL = True
except Exception:
    HAS_PIL = False


_STEP2_SELF = """【第二步：填写用户】
· 用户A = 当前扫码登录的账号，登录成功后自动填入，不可修改。
  想查别人和你的互动，就用你自己的账号扫码登录。
· 用户B 支持三种写法：
  · 微博昵称（如：owdeky）
  · 主页链接（如：https://weibo.com/u/1234567890）
  · 数字 uid
提示：如果对方改过昵称，直接填 uid 或主页链接最稳。
"""

_STEP2_MANUAL = """【第二步：填写用户】
用户A / 用户B 支持三种写法：
  · 微博昵称（如：owdeky）
  · 主页链接（如：https://weibo.com/u/1234567890）
  · 数字 uid
提示：如果对方改过昵称，直接填 uid 或主页链接最稳。
"""

HELP_TEXT = (
    """微博互动查询器 · 使用说明

【功能】
"""
    + (
        "提取「你（当前登录账号）」与另一个微博用户之间的互动记录："
        if SELF_IS_A
        else "提取两个微博用户之间的互动记录："
    )
    + """
转发 / 评论 / 评论回复 / 点赞，结果导出为 HTML 日志（+ 可选 Excel / CSV）。

【第一步：登录】
点击「扫码登录微博」，用微博 App 扫描二维码并确认。
登录态会保存，下次打开自动使用；失效时会自动弹出二维码重新登录。

"""
    + (_STEP2_SELF if SELF_IS_A else _STEP2_MANUAL)
    + """
【第三步：设置】
· 时间范围：最近 7/30/90/180/365 天，或选「自定义」输入起止日期（YYYY-MM-DD，结束留空=今天）
· 互动类型：勾选要查的互动（转发 / 评论 / 点赞）
· 抓取速度：慢最稳，快更省时但容易触发微博风控
· 导出格式：可只勾选需要的格式（HTML 日志 / Excel / CSV）

【开始抓取】
点「开始抓取」后耐心等待。期间可点「暂停/继续」。
抓取完成会自动打开 HTML 日志，也可点「打开最新日志 / 打开结果文件夹」查看。

【常见问题】
Q：为什么有时点赞很少或没有？
A：微博点赞列表接口不稳定，接口不可用时程序会自动暂停点赞扫描，并在每 50 条微博后自动重试；结果可能不完整。
Q：为什么评论可能不全？
A：评论区有折叠/拉黑隐藏机制，程序用「热度+时间」双排序抓取尽量覆盖，但极端情况下仍有遗漏。
Q：有微博抓取失败怎么办？
A：失败会先自动重试 3 次；仍失败的在 HTML 顶部有提示，可稍后重新抓取（程序会自动跳过已抓到的记录）。
Q：抓取中断了怎么办？
A：程序自动保存断点，重新点「开始抓取」会从断点继续，不会重复扫描。
Q：抓取时登录失效？
A：程序会自动弹出新二维码，扫码后自动从断点继续。
"""
    + (
        """  注意：重新登录的账号必须和开始时一致（用户A 只能是登录账号）。
"""
        if SELF_IS_A
        else ""
    )
)


class App:
    """主窗口。所有界面状态都挂在实例上，后台线程只通过消息队列与它通信。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("微博互动查询器")
        root.geometry("760x720")
        root.minsize(700, 640)
        # 后台线程 → 主线程的消息通道，元素为 (kind, payload...)
        self.msg_q: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.session: requests.Session | None = None
        # 当前登录账号信息 {"uid", "screen_name"}：
        # self 版「用户A」固定取这里，不读用户输入。
        self.self_info: UserInfo = {"uid": "", "screen_name": ""}
        self.qr_photo: Any = None  # 持有 PhotoImage 引用，否则会被 GC 掉导致二维码不显示
        self.pause_event = threading.Event()
        self.pause_event.set()  # 默认运行中；clear = 暂停
        self.stop_event = threading.Event()  # set = 请求结束抓取（保留已扫到的部分）
        self.settings_path = os.path.join(config.DATA_DIR, "settings.json")
        # 只有用户A 取登录账号时才有 a_var（只读展示登录账号）
        self.a_var: tk.StringVar | None = None
        self.type_vars: dict[str, tk.BooleanVar] = {}
        self.fmt_vars: dict[str, tk.BooleanVar] = {}
        self._build_ui()
        self._load_settings()
        self._init_checkpoint()
        self.root.after(150, self._poll_queue)
        # 自动尝试已保存登录态
        threading.Thread(target=self._auto_login, daemon=True).start()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        pad: dict[str, Any] = {"padx": 12, "pady": 6}
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        title = ttk.Label(outer, text="微博互动查询器", font=("Microsoft YaHei", 16, "bold"))
        title.pack(anchor="w", **pad)
        subtitle = (
            "提取「你（登录账号）」与另一个微博用户之间的互动记录"
            "（转发 / 评论 / 评论回复 / 点赞），结果导出 Excel + HTML 日志。"
            if SELF_IS_A
            else "提取两个微博用户之间的互动记录"
            "（转发 / 评论 / 评论回复 / 点赞），结果导出 Excel + HTML 日志。"
        )
        ttk.Label(outer, text=subtitle, foreground="#666").pack(anchor="w", padx=12)

        # --- 登录区 ---
        login_frame = ttk.LabelFrame(outer, text=" 1. 登录微博（扫码） ", padding=10)
        login_frame.pack(fill="x", **pad)
        self.login_btn = ttk.Button(login_frame, text="扫码登录微博", command=self._do_login)
        self.login_btn.pack(side="left")
        self.login_state = ttk.Label(login_frame, text="未登录", foreground="#c00")
        self.login_state.pack(side="left", padx=12)
        self.qr_label = ttk.Label(login_frame, text="")
        self.qr_label.pack(side="left", padx=12)

        # --- 用户区 ---
        # SELF_IS_A：用户A 固定 = 扫码登录的账号（只读输入框，自动填入），只需填 B
        # 否则两个用户都从输入框读取
        user_frame = ttk.LabelFrame(
            outer, text=" 2. 选择要对比的用户 " if SELF_IS_A else " 2. 填写两个微博用户 ", padding=10
        )
        user_frame.pack(fill="x", **pad)
        ttk.Label(user_frame, text="用户A：").grid(row=0, column=0, sticky="e")
        if SELF_IS_A:
            self.a_var = tk.StringVar(value="（未登录）")
            self.entry_a = ttk.Entry(user_frame, width=34, textvariable=self.a_var, state="readonly")
            self.entry_a.grid(row=0, column=1, sticky="w", pady=3)
            ttk.Label(user_frame, text="← 当前扫码登录的账号，不可修改", foreground="#888").grid(
                row=0, column=2, sticky="w", padx=6
            )
        else:
            self.a_var = None
            self.entry_a = ttk.Entry(user_frame, width=34)
            self.entry_a.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(user_frame, text="用户B：").grid(row=1, column=0, sticky="e")
        self.entry_b = ttk.Entry(user_frame, width=34)
        self.entry_b.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(
            user_frame, text="支持：昵称 / 微博主页链接 / 数字 uid（可右键复制/粘贴）", foreground="#888"
        ).grid(row=2, column=1, columnspan=2, sticky="w")

        # --- 参数区 ---
        opt_frame = ttk.LabelFrame(outer, text=" 3. 抓取范围与类型 ", padding=10)
        opt_frame.pack(fill="x", **pad)
        ttk.Label(opt_frame, text="时间范围：").grid(row=0, column=0, sticky="e")
        self.days_var = tk.StringVar(value="30")
        days_cb = ttk.Combobox(
            opt_frame,
            textvariable=self.days_var,
            state="readonly",
            width=10,
            values=["7", "30", "90", "180", "365", "自定义"],
        )
        days_cb.grid(row=0, column=1, sticky="w", pady=3)
        days_cb.bind("<<ComboboxSelected>>", self._on_days_change)
        ttk.Label(opt_frame, text="最近 N 天；选「自定义」可输入起止日期", foreground="#888").grid(
            row=0, column=2, sticky="w", padx=8
        )

        # 自定义日期行（默认禁用，选「自定义」时启用）
        self.date_row = ttk.Frame(opt_frame)
        self.date_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(self.date_row, text="开始日期：").pack(side="left")
        self.start_date_entry = ttk.Entry(self.date_row, width=12)
        self.start_date_entry.pack(side="left")
        ttk.Label(self.date_row, text="  结束日期：").pack(side="left")
        self.end_date_entry = ttk.Entry(self.date_row, width=12)
        self.end_date_entry.pack(side="left")
        ttk.Label(self.date_row, text="  格式 YYYY-MM-DD，结束日期留空 = 今天", foreground="#888").pack(
            side="left", padx=6
        )
        self._set_date_inputs_enabled(False)

        ttk.Label(opt_frame, text="互动类型：").grid(row=1, column=0, sticky="e")
        for i, t in enumerate(["转发", "评论（含评论回复）", "点赞"]):
            var = tk.BooleanVar(value=True)
            self.type_vars[t] = var
            ttk.Checkbutton(opt_frame, text=t, variable=var).grid(row=1, column=1 + i, sticky="w", padx=6)

        ttk.Label(opt_frame, text="抓取速度：").grid(row=2, column=0, sticky="e")
        self.speed_var = tk.StringVar(value="中")
        speed_cb = ttk.Combobox(
            opt_frame,
            textvariable=self.speed_var,
            state="readonly",
            width=8,
            values=["慢（最稳）", "中", "快（易风控）"],
        )
        speed_cb.grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(opt_frame, text="速度越快越容易触发微博风控，首次建议用「慢」", foreground="#888").grid(
            row=2, column=2, sticky="w", padx=8
        )

        ttk.Label(opt_frame, text="导出格式：").grid(row=4, column=0, sticky="e")
        for i, f in enumerate(["HTML日志", "Excel", "CSV"]):
            var = tk.BooleanVar(value=True)
            self.fmt_vars[f] = var
            ttk.Checkbutton(opt_frame, text=f, variable=var).grid(row=4, column=1 + i, sticky="w", padx=6)

        # --- 按钮 ---
        btn_frame = ttk.Frame(outer)
        btn_frame.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btn_frame, text="开始抓取", command=self._start)
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(btn_frame, text="暂停", command=self._toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(btn_frame, text="结束", command=self._request_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(btn_frame, text="打开最新日志", command=self._open_latest)
        self.open_btn.pack(side="left", padx=8)
        self.open_dir_btn = ttk.Button(btn_frame, text="打开结果文件夹", command=self._open_output_dir)
        self.open_dir_btn.pack(side="left", padx=8)
        self.help_btn = ttk.Button(btn_frame, text="帮助", command=self._show_help)
        self.help_btn.pack(side="left", padx=8)

        # --- 进度区 ---
        prog_frame = ttk.LabelFrame(outer, text=" 抓取进度 ", padding=8)
        prog_frame.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        self.eta_label = ttk.Label(prog_frame, text="尚未开始", foreground="#666")
        self.eta_label.pack(anchor="w", pady=(4, 0))

        # --- 日志区（加大 + 右侧滚动条）---
        log_frame = ttk.LabelFrame(outer, text=" 运行日志 ", padding=6)
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=22, state="disabled", wrap="word", font=("Consolas", 10))
        self.log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=self.log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_scroll.pack(side="right", fill="y")
        self._append_log("欢迎使用微博互动查询器。\n点击「扫码登录微博」，用微博 App 扫码后即可开始。")

        # 所有输入框支持右键剪切/复制/粘贴/全选
        for _e in (self.entry_a, self.entry_b, self.start_date_entry, self.end_date_entry):
            self._attach_entry_menu(_e)

    def _attach_entry_menu(self, entry: ttk.Entry) -> None:
        """给输入框挂上右键菜单（tkinter 的 Entry 默认没有右键菜单）。"""
        menu = tk.Menu(entry, tearoff=0)
        for label, ev in (
            ("剪切", "<<Cut>>"),
            ("复制", "<<Copy>>"),
            ("粘贴", "<<Paste>>"),
            ("全选", "<<SelectAll>>"),
        ):
            # 用 partial 而不是带默认参数的 lambda：既避开 mypy 无法推断
            # lambda 类型的问题，也避免闭包变量延迟绑定。
            menu.add_command(label=label, command=partial(self._entry_action, entry, ev))
        entry.bind("<Button-3>", partial(self._popup, menu))

    def _popup(self, menu: tk.Menu, event: tk.Event) -> None:
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _entry_action(self, entry: ttk.Entry, event: str) -> None:
        with contextlib.suppress(Exception):
            entry.focus_force()
            entry.event_generate(event)

    # ---------- 自定义日期 ----------
    def _set_date_inputs_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.start_date_entry.configure(state=state)
        self.end_date_entry.configure(state=state)

    def _on_days_change(self, event: tk.Event | None = None) -> None:
        self._set_date_inputs_enabled(self.days_var.get() == "自定义")

    # ---------- 配置记忆 ----------
    def _load_settings(self) -> None:
        """回填上次的配置。任何异常都静默——配置读不出来不该拦住程序启动。"""
        try:
            if not os.path.exists(self.settings_path):
                return
            with open(self.settings_path, encoding="utf-8") as f:
                s = json.load(f)
            if not SELF_IS_A:
                self.entry_a.insert(0, s.get("a", ""))
            # self 版：用户A 固定为登录账号，不记忆也不回填（旧配置里的 "a" 直接忽略）
            self.entry_b.insert(0, s.get("b", ""))
            if s.get("days"):
                self.days_var.set(s["days"])
                self._on_days_change()
            self.start_date_entry.insert(0, s.get("custom_start", ""))
            self.end_date_entry.insert(0, s.get("custom_end", ""))
            for k, v in (s.get("types") or {}).items():
                if k in self.type_vars and isinstance(v, bool):
                    self.type_vars[k].set(v)
            if s.get("speed"):
                self.speed_var.set(s["speed"])
            for k, v in (s.get("formats") or {}).items():
                if k in self.fmt_vars and isinstance(v, bool):
                    self.fmt_vars[k].set(v)
        except Exception:
            pass

    def _save_settings(self) -> None:
        """记忆本次配置（失败静默，不影响抓取）。"""
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            s: dict[str, Any] = {
                "b": self.entry_b.get().strip(),
                "days": self.days_var.get(),
                "custom_start": self.start_date_entry.get().strip(),
                "custom_end": self.end_date_entry.get().strip(),
                "types": {k: v.get() for k, v in self.type_vars.items()},
                "speed": self.speed_var.get(),
                "formats": {k: v.get() for k, v in self.fmt_vars.items()},
            }
            if not SELF_IS_A:
                s["a"] = self.entry_a.get().strip()
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 日志 ----------
    def _append_log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_queue(self) -> None:
        """主线程消息泵：消费后台线程投递的状态，更新界面。每 150 ms 跑一次。"""
        try:
            while True:
                item = self.msg_q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "qr":
                    self._show_qr(item[1])
                elif kind == "set_a":
                    # 记录当前登录账号信息（SELF_IS_A 时用于填只读的用户A，
                    # 否则仅用于日志展示）
                    info = item[1] or {}
                    self.self_info = {
                        "uid": info.get("uid") or "",
                        "screen_name": info.get("screen_name") or "",
                    }
                    if SELF_IS_A and self.a_var is not None:
                        uid = self.self_info["uid"]
                        if uid:
                            name = self.self_info["screen_name"] or "（未取到昵称）"
                            self.a_var.set(f"{name}（uid {uid}）")
                        else:
                            self.a_var.set("（未登录）")
                elif kind == "login_ok":
                    self.login_state.configure(text=f"已登录：{item[1]}", foreground="#080")
                    self.login_btn.configure(state="disabled")
                elif kind == "login_fail":
                    self.login_state.configure(text=item[1], foreground="#c00")
                    self.login_btn.configure(state="normal")
                elif kind == "done":
                    self._reset_task_buttons()
                    self.progress.configure(value=100)
                    self.eta_label.configure(text="抓取完成 ✔")
                    messagebox.showinfo("完成", item[1])
                elif kind == "err":
                    self._reset_task_buttons()
                    self.eta_label.configure(text="抓取出错", foreground="#c00")
                    messagebox.showerror("出错了", item[1])
                elif kind == "relogin":
                    self.login_state.configure(text="登录态失效，请重新扫码", foreground="#c00")
                    self._show_qr(item[1])
                elif kind == "prog":
                    text, frac = item[1], item[2]
                    self.progress.configure(value=min(100, frac))
                    self.eta_label.configure(text=text)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_queue)

    def _reset_task_buttons(self) -> None:
        """一次抓取结束（成功/出错）后把按钮与暂停状态复位。"""
        self.start_btn.configure(state="normal")
        self.login_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="暂停")
        self.stop_btn.configure(state="disabled", text="结束")
        self.pause_event.set()

    # ---------- 登录 ----------
    def _init_checkpoint(self) -> None:
        os.makedirs(config.DATA_DIR, exist_ok=True)

    @staticmethod
    def _fetch_self_info(session: requests.Session) -> UserInfo:
        """取登录账号信息 {"uid","screen_name"} —— self 版用户A 的唯一来源。"""
        try:
            return WeiboClient(session, speed=2).get_self_info()
        except Exception:
            return {"uid": "", "screen_name": ""}

    def _auto_login(self) -> None:
        """启动时尝试复用已保存的登录态（后台线程）。"""
        try:
            s = login_mod.load_cookies()
            if s is None:
                return
            self.session = s
            self.msg_q.put(("log", "[登录] 检测到已保存的登录态，直接使用。"))
            info = self._fetch_self_info(s)
            if info["uid"]:
                self.msg_q.put(("set_a", info))
                self.msg_q.put(("login_ok", info["screen_name"] or info["uid"]))
            else:
                self.msg_q.put(("login_ok", "（已保存登录态）"))
        except Exception as e:
            self.msg_q.put(("log", f"[登录] 自动登录失败：{e}"))

    def _do_login(self) -> None:
        if self.session is not None:
            messagebox.showinfo("提示", "已登录，无需重复登录。")
            return
        self.login_btn.configure(state="disabled")
        self.login_state.configure(text="正在获取二维码……", foreground="#a60")
        threading.Thread(target=self._login_worker, daemon=True).start()

    def _login_worker(self) -> None:
        """后台扫码登录（不能碰 tkinter，只能投消息）。"""

        def show_qr(path: str) -> None:
            self.msg_q.put(("qr", path))

        try:
            s = login_mod.qrcode_login(
                show_image_cb=show_qr,
                message_cb=lambda m: self.msg_q.put(("log", m)),
            )
            self.session = s
            login_mod.save_cookies(s)
            # 取当前登录账号信息（uid + 昵称）—— self 版的用户A 由此确定
            info = self._fetch_self_info(s)
            self.msg_q.put(("set_a", info))
            name = info["screen_name"] or info["uid"] or "已登录"
            self.msg_q.put(("log", "[登录] 扫码成功，登录微博账号。"))
            self.msg_q.put(("login_ok", name))
        except login_mod.LoginError as e:
            self.msg_q.put(("login_fail", f"登录失败：{e}"))
            self.msg_q.put(("log", f"[登录] 失败：{e}"))
        except Exception as e:
            self.msg_q.put(("login_fail", f"登录失败：{e}"))
            self.msg_q.put(("log", f"[登录] 异常：{traceback.format_exc()}"))

    def _show_qr(self, path: str) -> None:
        """把二维码图片显示到界面上（无 Pillow 时退回用系统看图程序打开）。"""
        if not HAS_PIL:
            self.login_state.configure(text="二维码已生成：" + path, foreground="#a60")
            with contextlib.suppress(Exception):
                os.startfile(path)
            return
        try:
            img = Image.open(path)
            img.thumbnail((150, 150))
            self.qr_photo = ImageTk.PhotoImage(img)
            self.qr_label.configure(image=self.qr_photo, text="")
            self.login_state.configure(text="请用微博 App 扫码并确认", foreground="#a60")
        except Exception:
            self.login_state.configure(text="二维码已生成：" + path, foreground="#a60")

    # ---------- 抓取 ----------
    def _start(self) -> None:
        """校验输入并启动后台抓取线程。"""
        if self.session is None:
            messagebox.showwarning("提示", "请先扫码登录微博。")
            return

        a: str | None
        if SELF_IS_A:
            if not self.self_info.get("uid"):
                messagebox.showwarning(
                    "提示", "还没拿到登录账号信息。请稍候；若长时间没有反应，请重新扫码登录。"
                )
                return
            # 用户A 固定 = 当前登录账号（不读输入框，输入框是只读展示）
            a = None
            b = self.entry_b.get().strip()
            if not b:
                messagebox.showwarning("提示", "请填写用户B（昵称 / 主页链接 / uid）。")
                return
            if b == self.self_info["uid"] or (
                self.self_info.get("screen_name") and b == self.self_info["screen_name"]
            ):
                messagebox.showwarning("提示", "用户B 不能是当前登录账号本人。")
                return
        else:
            a = self.entry_a.get().strip()
            b = self.entry_b.get().strip()
            if not a or not b:
                messagebox.showwarning("提示", "请填写用户A和用户B（昵称 / 主页链接 / uid）。")
                return
            if a.lower() == b.lower():
                messagebox.showwarning("提示", "用户A和用户B不能是同一个。")
                return

        types: set[str] = set()
        if self.type_vars["转发"].get():
            types.add("转发")
        if self.type_vars["评论（含评论回复）"].get():
            types.add("评论")
        if self.type_vars["点赞"].get():
            types.add("点赞")
        if not types:
            messagebox.showwarning("提示", "请至少勾选一种互动类型。")
            return

        formats: set[str] = set()
        fmt_map = {"HTML日志": "html", "Excel": "excel", "CSV": "csv"}
        for f, var in self.fmt_vars.items():
            if var.get():
                formats.add(fmt_map[f])
        if not formats:
            messagebox.showwarning("提示", "请至少勾选一种导出格式。")
            return

        self._save_settings()  # 记忆本次配置，下次自动填充

        speed_map = {"慢（最稳）": 1, "中": 2, "快（易风控）": 3}
        speed = speed_map[self.speed_var.get()]
        days = self.days_var.get()

        # 自定义日期校验
        if days == "自定义":
            start_s = self.start_date_entry.get().strip()
            if not start_s:
                messagebox.showwarning("提示", "请填写开始日期（YYYY-MM-DD）。")
                return
            try:
                datetime.strptime(start_s, "%Y-%m-%d")
            except ValueError:
                messagebox.showwarning("提示", "开始日期格式应为 YYYY-MM-DD，例如 2025-06-01。")
                return
            end_s = self.end_date_entry.get().strip()
            if end_s:
                try:
                    datetime.strptime(end_s, "%Y-%m-%d")
                except ValueError:
                    messagebox.showwarning("提示", "结束日期格式应为 YYYY-MM-DD，例如 2025-08-31。")
                    return

        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="暂停")
        self.stop_btn.configure(state="normal", text="结束")
        self.pause_event.set()  # 新任务开始：确保未处于暂停态
        self.stop_event.clear()  # 新任务开始：清掉上次的结束请求
        a_label: str
        if SELF_IS_A:
            a_label = (self.self_info.get("screen_name") or self.self_info["uid"]) + "（登录账号）"
        else:
            a_label = str(a)
        self._append_log("\n" + "=" * 46)
        self._append_log(f"开始抓取：{a_label} × {b}（最近 {days} 天，速度：{self.speed_var.get()}）")
        threading.Thread(
            target=self._fetch_worker,
            args=(a, b, types, speed, days, formats),
            daemon=True,
        ).start()

    def _toggle_pause(self) -> None:
        """暂停/继续抓取。"""
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text="继续")
            self.eta_label.configure(text="已暂停 —— 点击「继续」恢复扫描", foreground="#a60")
        else:
            self.pause_event.set()
            self.pause_btn.configure(text="暂停")
            self.eta_label.configure(text="正在恢复扫描……", foreground="#666")

    def _request_stop(self) -> None:
        """请求结束抓取：停止扫描，导出当前已扫到的部分。"""
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self.pause_event.set()  # 若正处于暂停，先恢复让循环走到停止检查
        self.stop_btn.configure(state="disabled", text="结束")
        self.pause_btn.configure(state="disabled", text="暂停")
        self._append_log("已请求结束：扫描将在当前微博处停止，已扫到的记录照常导出……")

    def _fetch_worker(
        self, a: str | None, b: str, types: set[str], speed: int, days: str, formats: set[str]
    ) -> None:
        """抓取主流程（后台线程）。

        a 为 None 表示 self 版（用户A = 当前登录账号）；否则为用户手填的 A。
        """
        checkpoint = None  # 解析用户后按「用户对」隔离的断点文件，支持增量刷新
        scan_start: float | None = None  # 扫描阶段开始时间（用于预计剩余时间）
        try:
            if self.session is None:
                self.msg_q.put(("err", "尚未登录，请先扫码登录。"))
                return
            session = self.session
            client = WeiboClient(session, speed=speed)

            self.msg_q.put(("log", "正在解析用户信息……"))
            login_uid = ""
            if SELF_IS_A:
                # 用户A = 当前登录账号（本版本固定，不从输入框取）
                info = self.self_info if self.self_info.get("uid") else client.get_self_info()
                if not info.get("uid"):
                    self.msg_q.put(
                        ("err", "无法获取当前登录账号的 uid（登录态可能已失效）。请重新扫码登录后再试。")
                    )
                    return
                self.msg_q.put(("set_a", info))
                user_a = UserInfo(uid=info["uid"], screen_name=info.get("screen_name") or "我")
                user_b = client.resolve_user(b)
                if user_a["uid"] == user_b["uid"]:
                    self.msg_q.put(("err", "用户B 不能是当前登录账号本人（查自己与自己的互动没有意义）。"))
                    return
                self.msg_q.put(
                    ("log", f"用户A：{user_a['screen_name']} (uid {user_a['uid']})  ← 当前登录账号")
                )
                self.msg_q.put(("log", f"用户B：{user_b['screen_name']} (uid {user_b['uid']})"))
                # 登录账号 uid（用于提取自己微博下的待审核评论）—— 就是用户A 本人
                login_uid = user_a["uid"]
                self.msg_q.put(("log", f"登录账号 uid：{login_uid}（将同时检查自己微博的待审核评论）"))
            else:
                if not a:
                    self.msg_q.put(("err", "缺少用户A。"))
                    return
                user_a = client.resolve_user(a)
                user_b = client.resolve_user(b)
                if user_a["uid"] == user_b["uid"]:
                    self.msg_q.put(("err", "用户A 和用户B 不能是同一个。"))
                    return
                self.msg_q.put(("log", f"用户A：{user_a['screen_name']} (uid {user_a['uid']})"))
                self.msg_q.put(("log", f"用户B：{user_b['screen_name']} (uid {user_b['uid']})"))
                # 当前登录账号 uid（用于提取自己微博下的待审核评论）
                login_uid = client.get_self_uid()
                if login_uid:
                    self.msg_q.put(("log", f"登录账号 uid：{login_uid}（将同时检查自己微博的待审核评论）"))

            now = datetime.now(CST)
            if days == "自定义":
                start_s = self.start_date_entry.get().strip()
                start_ts = parse_date_input(start_s)
                end_s = self.end_date_entry.get().strip()
                end_ts = parse_date_input(end_s) if end_s else now.timestamp()
                self.msg_q.put(("log", f"时间范围：{start_s} ~ {end_s or '今天'}"))
            else:
                end_ts = now.timestamp()
                start_ts = (now - timedelta(days=int(days))).timestamp()
                self.msg_q.put(("log", f"时间范围：{now.strftime('%Y-%m-%d %H:%M')} 往前 {days} 天"))

            def progress(msg: str) -> None:
                self.msg_q.put(("log", msg))
                nonlocal scan_start
                # 解析扫描进度 "[A→B 扫描] 12/3214" / "[B→A 扫描] 500/3214"
                m = re.search(r"扫描\]\s*(\d+)/(\d+)", msg)
                if m:
                    done = int(m.group(1))
                    total = int(m.group(2))
                    if total <= 0:
                        return
                    frac = done * 100.0 / total
                    if done <= 0 or scan_start is None:
                        self.msg_q.put(
                            ("prog", f"已扫描 {done}/{total}（{frac:.0f}%）· 正在估算时间……", frac)
                        )
                        return
                    elapsed = time.time() - scan_start
                    rate = done / elapsed  # 条/秒
                    remain = (total - done) / rate if rate > 0 else 0
                    eta_text = f"{remain / 60:.0f} 分钟" if remain >= 60 else f"{remain:.0f} 秒"
                    spent_text = f"{elapsed / 60:.0f} 分钟" if elapsed >= 60 else f"{elapsed:.0f} 秒"
                    self.msg_q.put(
                        (
                            "prog",
                            f"已扫描 {done}/{total}（{frac:.0f}%）· 已用 {spent_text} · 预计还需 ~{eta_text}",
                            frac,
                        )
                    )

            # 按「用户对」隔离断点：同一对用户再次抓取时增量刷新
            # （重扫上次扫描回退 3 天内的旧微博，防止旧微博的新互动漏掉）
            # 存储为 SQLite（checkpoint.py）；若同目录存在旧的同名 .json 断点，
            # 首次使用会自动迁移，原文件保留不动。
            cp_dir = os.path.join(config.DATA_DIR, "checkpoints")
            os.makedirs(cp_dir, exist_ok=True)
            checkpoint = os.path.join(cp_dir, f"checkpoint_{user_a['uid']}_{user_b['uid']}.db")

            self.msg_q.put(("log", "开始扫描互动（微博多时请耐心等待）……"))
            self.msg_q.put(("prog", "正在抓取微博列表……", 0))
            scan_start = None
            analyzer: InteractionAnalyzer
            records: list[InteractionRecord] = []
            while True:
                try:
                    analyzer = InteractionAnalyzer(
                        client,
                        user_a,
                        user_b,
                        types=types,
                        deep_replies=True,
                        speed=speed,
                        progress_cb=progress,
                        checkpoint=checkpoint,
                        login_uid=login_uid,
                    )
                    if scan_start is None:
                        scan_start = time.time()
                    records = analyzer.run(
                        start_ts, end_ts, pause_event=self.pause_event, stop_event=self.stop_event
                    )
                    break
                except NotLoggedInError:
                    self.msg_q.put(("log", "[登录] 登录态已失效，正在弹出二维码重新登录……"))
                    new_s = self._relogin_blocking()
                    if new_s is None:
                        self.msg_q.put(
                            (
                                "err",
                                "重新扫码登录未完成，抓取已中止（断点已保存，可再次点击「开始抓取」继续）。",
                            )
                        )
                        return
                    self.session = new_s
                    login_mod.save_cookies(new_s)
                    client = WeiboClient(new_s, speed=speed)
                    if SELF_IS_A:
                        # 用户A 只能是登录账号：若重新登录换了账号，A 就变了，
                        # 断点里的记录和 scanned_mids 都不再对应，必须中止而不是继续。
                        new_info = client.get_self_info()
                        if new_info["uid"] and new_info["uid"] != user_a["uid"]:
                            self.msg_q.put(
                                (
                                    "err",
                                    f"重新登录的账号（{new_info['screen_name'] or new_info['uid']}）"
                                    f"与开始时不一致。\n用户A 只能是登录账号，"
                                    f"请用同一个账号登录后重新点「开始抓取」。",
                                )
                            )
                            return
                        self.msg_q.put(("set_a", new_info))
                        self.msg_q.put(("log", "[登录] 重新登录成功（账号一致），从断点继续抓取……"))
                    else:
                        login_uid = client.get_self_uid()
                        self.msg_q.put(("log", "[登录] 重新登录成功，从断点继续抓取……"))
                    continue
                except RiskControlError as e:
                    self.msg_q.put(("log", f"[风控] {e}，等待 180 秒后自动继续……"))
                    self.msg_q.put(("prog", "触发风控，等待 180 秒后自动继续……", 0))
                    time.sleep(180)

            result = export(
                records,
                user_a,
                user_b,
                out_dir=os.path.join(config.DATA_DIR, "output"),
                formats=formats,
                failed_weibos=analyzer.failed_weibos,
                enable_refresh=True,
            )
            manual_stop = self.stop_event.is_set()
            if manual_stop:
                self.msg_q.put(("log", "已手动结束：导出当前已扫描到的部分。"))
            self.msg_q.put(("log", f"发现互动记录 {result['count']} 条"))
            for key, label in (("html", "HTML日志"), ("excel", "Excel"), ("csv", "CSV")):
                if result.get(key):
                    self.msg_q.put(("log", f"{label}：{result[key]}"))
            if not analyzer.like_api_ok:
                self.msg_q.put(
                    ("log", "提示：点赞接口本次中途不可用（已自动暂停并周期重试），点赞数据可能不完整。")
                )
            if analyzer.failed_weibos:
                self.msg_q.put(
                    (
                        "log",
                        f"提示：有 {len(analyzer.failed_weibos)} 条微博抓取失败（已重试），"
                        f"其互动可能缺失，详见 HTML 顶部提示。",
                    )
                )
            fail_note = (
                f"\n有 {len(analyzer.failed_weibos)} 条微博抓取失败，详见日志顶部。"
                if analyzer.failed_weibos
                else ""
            )
            if manual_stop:
                self.msg_q.put(
                    ("done", f"已手动结束，共导出 {result['count']} 条互动记录（已扫到的部分）。{fail_note}")
                )
            else:
                self.msg_q.put(("done", f"抓取完成，共 {result['count']} 条互动记录。{fail_note}"))
            if result.get("html"):
                html_path = str(result["html"])
                threading.Thread(
                    target=lambda: webbrowser.open("file:///" + html_path.replace("\\", "/")), daemon=True
                ).start()
        except Exception as e:
            self.msg_q.put(("err", str(e)))
            self.msg_q.put(("log", traceback.format_exc()))

    def _relogin_blocking(self) -> requests.Session | None:
        """登录失效时阻塞式重新扫码；成功返回新 session，失败返回 None。"""

        def show_qr(path: str) -> None:
            self.msg_q.put(("relogin", path))

        try:
            s = login_mod.qrcode_login(
                show_image_cb=show_qr,
                message_cb=lambda m: self.msg_q.put(("log", m)),
            )
            info = self._fetch_self_info(s)
            self.msg_q.put(("set_a", info))
            name = info["screen_name"] or info["uid"] or "已登录"
            self.msg_q.put(("log", f"[登录] 重新扫码成功：{name}"))
            return s
        except login_mod.LoginError as e:
            self.msg_q.put(("log", f"[登录] 重新扫码失败：{e}"))
            return None
        except Exception as e:
            self.msg_q.put(("log", f"[登录] 重新扫码异常：{e}"))
            return None

    # ---------- 打开最新日志 / 结果文件夹 / 帮助 ----------
    def _open_latest(self) -> None:
        out = os.path.join(config.DATA_DIR, "output")
        if not os.path.isdir(out):
            messagebox.showinfo("提示", "还没有生成过日志。")
            return
        files = [f for f in os.listdir(out) if f.endswith(".html")]
        if not files:
            messagebox.showinfo("提示", "还没有生成过日志。")
            return
        latest = os.path.join(out, sorted(files)[-1])
        webbrowser.open("file:///" + latest.replace("\\", "/"))

    def _open_output_dir(self) -> None:
        out = os.path.join(config.DATA_DIR, "output")
        if not os.path.isdir(out):
            messagebox.showinfo("提示", "还没有生成过结果。")
            return
        try:
            os.startfile(out)
        except Exception as e:
            messagebox.showerror("出错了", f"无法打开文件夹：{e}")

    def _show_help(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("帮助 · 使用说明")
        win.geometry("620x520")
        txt = tk.Text(win, wrap="word", font=("Microsoft YaHei", 10), padx=14, pady=10)
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=8)


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
