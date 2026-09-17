"""微博互动日志 · 本地刷新服务

用浏览器打开 http://127.0.0.1:8000 查看互动日志。
页面上的「刷新数据」按钮会重新抓取「今天 → 2025-11-01」的互动并自动刷新页面。

用法：
  python server.py            # 启动服务（默认端口 8000）
  python server.py --port 9000

说明：本服务只监听 127.0.0.1，抓取动作是把 ``main.py`` 作为**子进程**拉起，
因此刷新请求本身不持有登录态，也不对外暴露任何接口。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypedDict

with contextlib.suppress(Exception):
    # TextIOWrapper 才有 reconfigure；stdout 被重定向成别的对象时会缺这个属性
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

import config

OUTPUT_DIR: str = config.OUTPUT_DIR
PROJECT_DIR: str = getattr(config, "PROJECT_DIR", os.path.dirname(os.path.abspath(__file__)))
START_DATE: str = "2025-11-01"


class ServerState(TypedDict):
    """刷新服务的运行时状态（单进程、单实例）。"""

    running: bool
    started_at: float | None
    proc: subprocess.Popen[bytes] | None
    log_path: str
    checkpoint: str


state: ServerState = {
    "running": False,
    "started_at": None,
    "proc": None,
    "log_path": os.path.join(config.DATA_DIR, "refresh_run.log"),
    "checkpoint": os.path.join(config.DATA_DIR, "checkpoint.json"),
}


def latest_html() -> str | None:
    """返回 output 目录里最新的 .html 文件名（不含目录）。"""
    if not os.path.isdir(OUTPUT_DIR):
        return None
    files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".html")]
    if not files:
        return None
    files.sort(key=lambda f: os.path.getmtime(os.path.join(OUTPUT_DIR, f)), reverse=True)
    return files[0]


def checkpoint_progress() -> tuple[int | None, int | None]:
    """从断点文件读 (已扫微博数, 已发现记录数)；读不到时返回 (None, None)。"""
    try:
        with open(state["checkpoint"], encoding="utf-8") as f:
            cp = json.load(f)
        return len(cp.get("scanned_mids", [])), len(cp.get("records", []))
    except Exception:
        return None, None


def start_refresh() -> tuple[bool, str]:
    """拉起一次抓取子进程。返回 (是否启动成功, 提示信息)。"""
    if state["running"]:
        return False, "已有抓取任务在运行"
    today = datetime.now().strftime("%Y-%m-%d")
    cmd = [
        sys.executable,
        os.path.join(PROJECT_DIR, "main.py"),
        "--u1",
        "owdeky",
        "--u2",
        "高级的EAUX_花枝Hanae仙人",
        "--start",
        START_DATE,
        "--end",
        today,
        "--speed",
        "2",
        "--types",
        "repost,comment,like",
        "--resume",
        "--checkpoint",
        state["checkpoint"],
    ]
    # 日志文件必须活到子进程结束，不能用 with（with 会在 Popen 返回后立刻关掉句柄）
    logf = open(state["log_path"], "w", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(
        cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=PROJECT_DIR, creationflags=0x00000008
    )  # 0x8 = DETACHED_PROCESS
    state["running"] = True
    state["started_at"] = time.time()
    state["proc"] = proc

    def _monitor() -> None:
        proc.wait()
        state["running"] = False

    threading.Thread(target=_monitor, daemon=True).start()
    return True, "已开始"


class Handler(SimpleHTTPRequestHandler):
    """把 OUTPUT_DIR 当站点根目录，并额外提供 /refresh、/status、/api/latest。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=OUTPUT_DIR, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # 静默：终端输出留给抓取日志

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/refresh":
            ok, msg = start_refresh()
            self._json({"ok": ok, "error": None if ok else msg})
            return
        if path == "/status":
            scanned, records = checkpoint_progress()
            self._json(
                {
                    "running": state["running"],
                    "scanned": scanned,
                    "records": records,
                    "latest_html": latest_html(),
                    "start": START_DATE,
                }
            )
            return
        if path in ("/", "/index.html"):
            html = latest_html()
            if html:
                self.send_response(302)
                self.send_header("Location", "/" + html)
                self.end_headers()
            else:
                self._json({"ok": False, "error": "还没有生成任何 HTML 日志"})
            return
        if path == "/api/latest":
            self._json({"latest_html": latest_html(), "running": state["running"]})
            return
        super().do_GET()

    def _json(self, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    ap = argparse.ArgumentParser(description="微博互动日志本地服务")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"服务已启动：{url}")
    print(f"页面上的「刷新数据」会重新抓取 {START_DATE} ~ 今天 的互动并自动刷新页面。")
    print("按 Ctrl+C 停止服务。")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
