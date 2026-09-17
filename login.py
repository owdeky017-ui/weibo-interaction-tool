"""扫码登录微博网页版，并做 cookie 持久化。

流程（当前 passport 的 v2 扫码接口）：
  1. GET  /sso/v2/qrcode/image  获取二维码图片 URL 与 qrid
  2. 下载二维码图片并展示给用户
  3. 轮询 GET /sso/v2/qrcode/check  等待手机扫码并确认
  4. 成功后访问返回的登录 URL，种下 SUB/SUBP 等 cookie
备用方式：手动粘贴浏览器 Cookie 字符串。

cookie 持久化默认用 **Windows DPAPI 加密**（见文件末尾「cookie 持久化」一节）：
  早期版本把含 SUB/SUBP 的登录态明文写在 data/cookies.json，
  任何能读到这个文件的人都能直接冒用账号。DPAPI 用当前 Windows 用户的
  主密钥加密，换机器 / 换用户都解不开，且不需要我们自己管密钥。
  旧的明文文件会被自动识别并就地升级为加密格式，不影响已有登录态。

接口说明只用于本程序自身的登录用途。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from http.cookiejar import Cookie
from typing import Any, cast

import requests

import config


class LoginError(Exception):
    """登录过程中的错误。"""


def _check_logged_in(session: requests.Session) -> bool | None:
    """验证登录态。返回 True=已登录；False=明确未登录；None=不确定（疑似风控/网络）。

    未登录访问 /ajax/profile/info 返回 403 Forbidden；
    已登录返回 {"ok":1,"data":{"user":{...}}}；
    风控限流时返回 418，此时登录态可能仍有效，不能据此判定未登录。
    """
    for _ in range(3):
        try:
            r = session.get(
                "https://weibo.com/ajax/profile/info",
                params={"uid": 1},
                timeout=15,
                allow_redirects=False,
            )
            if r.status_code == 200:
                data = r.json()
                return bool(data.get("ok") == 1 and (data.get("data") or {}).get("user"))
            if r.status_code == 403:
                return False
            # 418 等：疑似风控限流，稍等重试
        except Exception:
            pass
        time.sleep(3)
    return None


def qrcode_login(
    show_image_cb: Callable[[str], None] | None = None, message_cb: Callable[[str], None] | None = None
) -> requests.Session:
    """完整扫码登录，返回已登录的 session。

    show_image_cb: 回调，接收二维码 png 文件路径，用于把二维码展示给用户。
    message_cb: 回调，接收日志字符串（用于 GUI 显示自动刷新等提示）。
    二维码有效期约 3 分钟，过期或超时会自动重新生成新二维码，
    最多自动刷新 6 次，期间无需用户手动操作。
    """

    def _say(msg: str) -> None:
        print(f"[登录] {msg}")
        if message_cb:
            with contextlib.suppress(Exception):
                message_cb(f"[登录] {msg}")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Referer": "https://passport.weibo.com/",
        }
    )
    # 先访问登录页，拿到基础 cookie（X-CSRF-TOKEN 等）
    session.get(
        "https://passport.weibo.com/sso/signin"
        "?entry=miniblog&source=miniblog&url=https%3A%2F%2Fweibo.com%2F&guest_login=1",
        timeout=15,
    )

    max_tries = 6
    for attempt in range(1, max_tries + 1):
        if attempt > 1:
            _say(f"二维码已过期，自动刷新新二维码（第 {attempt}/{max_tries} 次）……")

        # ---------- 获取二维码 ----------
        qr_params: dict[str, str | int] = {"entry": "miniblog", "size": 180}
        resp = session.get(
            "https://passport.weibo.com/sso/v2/qrcode/image",
            params=qr_params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("retcode") != 20000000:
            if attempt == max_tries:
                raise LoginError(f"获取二维码失败: {data.get('msg') or data}")
            _say(f"获取二维码失败({data.get('msg')})，将重试……")
            time.sleep(2)
            continue
        qrid = data["data"]["qrid"]
        img_url = data["data"]["image"]

        # 下载二维码图片
        img = session.get(img_url, timeout=15)
        img.raise_for_status()
        os.makedirs(config.DATA_DIR, exist_ok=True)
        qr_path = os.path.join(config.DATA_DIR, "qrcode.png")
        with contextlib.suppress(Exception):
            os.remove(qr_path)
        with open(qr_path, "wb") as f:
            f.write(img.content)
        if show_image_cb:
            show_image_cb(qr_path)
        else:
            print(f"[登录] 二维码已保存到：{qr_path}")
            try:
                import subprocess

                subprocess.Popen(["cmd", "/c", "start", "", qr_path], shell=False)
                print("[登录] 已自动打开二维码图片，请用微博 App 扫码确认。")
            except Exception:
                print("[登录] 请手动打开上面的二维码图片，用微博 App 扫码确认。")

        # ---------- 轮询扫码状态 ----------
        _say("请在 3 分钟内用微博手机客户端扫码并确认……（二维码过期会自动刷新）")
        last_state: str | None = None
        deadline = time.time() + 180
        ok = False
        while time.time() < deadline:
            if deadline - time.time() < 20 and last_state != "expiring":
                _say("二维码即将过期，将自动刷新新二维码，请尽快扫码……")
                last_state = "expiring"
            try:
                r = session.get(
                    "https://passport.weibo.com/sso/v2/qrcode/check",
                    params={
                        "entry": "miniblog",
                        "source": "miniblog",
                        "url": "https://weibo.com/",
                        "qrid": qrid,
                        "disp": "",
                        "rid": "norid",
                        "ver": "20250520",
                    },
                    timeout=15,
                )
                d = r.json()
            except Exception:
                time.sleep(2)
                continue

            retcode = d.get("retcode")
            if retcode == 20000000:  # 扫码确认成功
                login_url = (d.get("data") or {}).get("url")
                if login_url:
                    session.get(login_url, timeout=15, allow_redirects=True)
                ok = True
                break
            elif retcode == 50114002:  # 已扫码未确认
                if last_state != "scanned":
                    _say("已扫码，请在手机上点击确认……")
                    last_state = "scanned"
            elif retcode in (50114003, 50114004, 50114015):  # 二维码过期/出错
                break
            # 50114001 未使用：继续等待
            time.sleep(2)
        if ok:
            break
        # 未确认成功（过期或 3 分钟超时）：进入下一轮自动刷新
        if attempt == max_tries:
            raise LoginError("二维码多次失效或超时，请稍后再试（检查手机网络）。")
        time.sleep(1)

    if _check_logged_in(session) is not True:
        raise LoginError("登录校验失败，请检查是否扫码成功或稍后重试。")
    _say("登录成功！")
    return session


def manual_login(cookie_str: str) -> requests.Session:
    """用浏览器复制的 Cookie 字符串登录。

    示例格式：SUB=xxx; SUBP=xxx; XSRF-TOKEN=xxx
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Referer": "https://weibo.com/",
        }
    )
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        session.cookies.set(k.strip(), v.strip(), domain=".weibo.com")
    if _check_logged_in(session) is not True:
        raise LoginError("Cookie 无效或已过期，请重新复制。")
    print("[登录] Cookie 验证通过！")
    return session


# ---------- cookie 持久化 ----------
#
# 存储格式（data/cookies.json）：
#   加密（默认，Windows）：
#     {"__weibo_enc__": 1, "alg": "dpapi", "data": "<base64(DPAPI blob)>"}
#   明文（仅在 DPAPI 不可用时退回，如非 Windows 平台）：
#     {"cookies": [ {...}, ... ]}
#
# 两种格式都能读：读到明文会自动加密后就地升级，用户无感。

_ENC_MARKER = "__weibo_enc__"
_DPAPI_ALG = "dpapi"
# 附加熵：把密文绑定到本程序，别的程序即便拿到 blob 也不能直接解密复用
_DPAPI_ENTROPY = b"weibo-interaction-query/cookies/v1"

# 最近一次 save_cookies 实际用的存储方式（"dpapi" / "plaintext"），供日志与测试观察
LAST_SAVE_MODE: str | None = None


def dpapi_available() -> bool:
    """当前环境是否能用 Windows DPAPI。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.crypt32 and ctypes.windll.kernel32)
    except Exception:
        return False


def _dpapi(data: bytes, protect: bool) -> bytes:
    """调用 CryptProtectData / CryptUnprotectData。失败抛 OSError。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(buf: Any) -> DATA_BLOB:
        return DATA_BLOB(len(buf), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    in_buf = ctypes.create_string_buffer(data, len(data))
    ent_buf = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    blob_in = _blob(in_buf)
    blob_ent = _blob(ent_buf)
    blob_out = DATA_BLOB()

    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # 0x1 = CRYPTPROTECT_UI_FORBIDDEN：不弹任何系统对话框（打包成 GUI 后尤其重要）
    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.GetLastError() or 0, "DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(blob_out.pbData)


def protect_bytes(raw: bytes) -> bytes | None:
    """加密；DPAPI 不可用或失败时返回 None（调用方决定是否退回明文）。"""
    if not dpapi_available():
        return None
    try:
        return _dpapi(raw, protect=True)
    except Exception:
        return None


def unprotect_bytes(blob: bytes) -> bytes | None:
    """解密；失败返回 None。"""
    if not dpapi_available():
        return None
    try:
        return _dpapi(blob, protect=False)
    except Exception:
        return None


def _cookie_dict_from_jar(jar: Iterable[Cookie]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in jar:
        out.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": c.secure,
                "expires": c.expires,
                # Cookie 的扩展字段在标准库类型里是私有的（_rest），只能 getattr
                "rest": dict(getattr(c, "_rest", None) or {}),
                "rfc2109": getattr(c, "rfc2109", False),
            }
        )
    return out


def save_cookies(session: requests.Session) -> str:
    """保存登录态。返回实际使用的存储方式（"dpapi" / "plaintext" / "failed"）。

    注意：这个函数**不抛异常**。保存失败只意味着下次要重新登录，
    不应该让一次成功的扫码登录白费。
    """
    global LAST_SAVE_MODE
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        # requests 的 RequestsCookieJar 同时继承 CookieJar 与 MutableMapping，
        # 类型桩里 __iter__ 的返回值有歧义，这里显式收敛成 Cookie 迭代器
        jar = cast("Iterable[Cookie]", session.cookies)
        payload = {"cookies": _cookie_dict_from_jar(jar)}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        blob = protect_bytes(raw)
        if blob is None:
            with open(config.COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            LAST_SAVE_MODE = "plaintext"
            print(
                "[登录] 提示：当前环境不支持 DPAPI，登录态以明文保存"
                "（仅限非 Windows 或系统加密组件异常时会出现）。"
            )
            return "plaintext"

        doc = {_ENC_MARKER: 1, "alg": _DPAPI_ALG, "data": base64.b64encode(blob).decode("ascii")}
        with open(config.COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        LAST_SAVE_MODE = "dpapi"
        return "dpapi"
    except Exception as e:
        LAST_SAVE_MODE = "failed"
        print(f"[登录] 警告：登录态保存失败（{type(e).__name__}: {e}），下次打开需要重新扫码。")
        return "failed"


def _read_cookie_doc(path: str) -> tuple[dict[str, Any], bool]:
    """读出 cookie 文档。返回 (payload, was_plaintext)。解密失败抛 LoginError。"""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise LoginError("登录态文件格式无法识别。")
    if not doc.get(_ENC_MARKER):
        return doc, True  # 旧的明文格式

    try:
        blob = base64.b64decode(doc.get("data") or "")
    except Exception as e:
        raise LoginError("登录态文件已损坏（base64 解析失败），请重新扫码登录。") from e
    raw = unprotect_bytes(blob)
    if raw is None:
        raise LoginError(
            "已保存的登录态无法解密。\n"
            "  · DPAPI 密文绑定当前 Windows 用户与机器，换机器 / 换账户 / 重装系统后解不开；\n"
            "  · 也可能是文件被改动过。\n"
            "  请删掉 data/cookies.json 后重新扫码登录。"
        )
    try:
        return json.loads(raw.decode("utf-8")), False
    except Exception as e:
        raise LoginError("登录态解密后内容异常，请重新扫码登录。") from e


def load_cookies() -> requests.Session | None:
    """从文件加载 cookie，返回已恢复的 session；失败返回 None。"""
    if not os.path.exists(config.COOKIE_FILE):
        return None
    try:
        payload, was_plaintext = _read_cookie_doc(config.COOKIE_FILE)

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Referer": "https://weibo.com/",
            }
        )
        for c in payload.get("cookies", []):
            try:
                ck = Cookie(
                    version=0,
                    name=c["name"],
                    value=c["value"],
                    port=None,
                    port_specified=False,
                    domain=c["domain"],
                    domain_specified=True,
                    domain_initial_dot=bool(c["domain"] and c["domain"].startswith(".")),
                    path=c.get("path", "/"),
                    path_specified=True,
                    secure=c.get("secure", False),
                    expires=c.get("expires"),
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest=c.get("rest", {}),
                    rfc2109=c.get("rfc2109", False),
                )
                session.cookies.set_cookie(ck)
            except Exception:
                continue

        result = _check_logged_in(session)
        if result is True:
            # 明文旧文件 → 就地升级为加密格式（升级失败不影响本次使用）
            if was_plaintext and dpapi_available():
                mode = save_cookies(session)
                if mode == "dpapi":
                    print("[登录] 已把明文登录态升级为 DPAPI 加密存储。")
            return session
        if result is None:
            raise LoginError(
                "检测到登录态仍有效但被微博限流（风控中），请等待几分钟后再运行；如持续失败可重新扫码。"
            )
        return None  # 明确未登录
    except LoginError:
        raise
    except Exception:
        pass
    return None


def login(
    show_image_cb: Callable[[str], None] | None = None, cookie_str: str | None = None
) -> requests.Session:
    """统一入口：优先 cookie 文件 → 手动 cookie → 扫码。返回登录 session。"""
    if cookie_str:
        return manual_login(cookie_str)
    s = load_cookies()
    if s:
        print("[登录] 使用已保存的登录态。")
        return s
    return qrcode_login(show_image_cb)
