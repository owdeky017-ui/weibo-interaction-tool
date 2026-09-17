"""cookie 持久化测试：Windows DPAPI 加密、完整性、明文升级、失败降级。

为什么值得单独一组用例：``data/cookies.json`` 里是 SUB/SUBP，等价于账号密码。
原实现是明文落盘——任何能读到该文件的人都能直接冒用账号。改成 DPAPI 后
有三个必须守住的边界：

1. 密文里**不能**残留任何明文（含 cookie 名）；
2. 密文被改动过必须解不开（完整性），而不是解出一段垃圾；
3. 解密失败要给**能照做的**提示，且**绝不**阻塞登录流程。

无 DPAPI 的环境（非 Windows）整体跳过。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import requests

import config
import login as login_mod

SECRET = "SUB-secret-token-do-not-leak-98765"

pytestmark = pytest.mark.skipif(not login_mod.dpapi_available(), reason="DPAPI 仅 Windows 可用")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_cookies`` 会请求微博校验登录态，测试里一律短路。"""
    monkeypatch.setattr(login_mod, "_check_logged_in", lambda s: True)


@pytest.fixture
def session_with_secret() -> requests.Session:
    s = requests.Session()
    s.cookies.set("SUB", SECRET, domain=".weibo.com")
    s.cookies.set("SUBP", "subp-value", domain=".weibo.com")
    s.cookies.set("XSRF-TOKEN", "xsrf", domain=".weibo.com")
    return s


def _cookie_file_text() -> str:
    return Path(config.COOKIE_FILE).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 加解密往返
# --------------------------------------------------------------------------- #


class TestProtectUnprotect:
    def test_round_trip(self) -> None:
        raw = b'{"cookies":[{"name":"SUB","value":"' + SECRET.encode() + b'"}]}'
        blob = login_mod.protect_bytes(raw)
        assert blob
        assert login_mod.unprotect_bytes(blob) == raw

    def test_ciphertext_contains_no_plaintext(self) -> None:
        raw = b'{"cookies":[{"name":"SUB","value":"' + SECRET.encode() + b'"}]}'
        blob = login_mod.protect_bytes(raw)
        assert blob is not None
        assert SECRET.encode() not in blob

    def test_payload_tampering_is_always_detected(self) -> None:
        """payload 区逐字节翻转，必须全部解密失败。

        跳过前 20 字节：DPAPI header（``dwFlags`` + description 等）本身
        不受完整性保护，改动它们不影响解密——这是算法行为，不是我们的漏洞。
        """
        blob = login_mod.protect_bytes(b"payload-to-tamper-with")
        assert blob is not None
        detected = 0
        total = 0
        for i in range(20, len(blob)):
            corrupted = bytearray(blob)
            corrupted[i] ^= 0x01
            total += 1
            if login_mod.unprotect_bytes(bytes(corrupted)) is None:
                detected += 1
        assert total > 0
        assert detected == total, f"只检出 {detected}/{total} 处篡改"

    def test_truncated_blob_fails(self) -> None:
        blob = login_mod.protect_bytes(b"abcdefghij")
        assert blob is not None
        assert login_mod.unprotect_bytes(blob[: len(blob) // 2]) is None

    @pytest.mark.parametrize("bad", [b"", b"not-a-dpapi-blob-at-all"])
    def test_garbage_blob_fails(self, bad: bytes) -> None:
        assert login_mod.unprotect_bytes(bad) is None

    def test_protect_returns_none_without_dpapi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(login_mod, "dpapi_available", lambda: False)
        assert login_mod.protect_bytes(b"x") is None


# --------------------------------------------------------------------------- #
# 落盘格式
# --------------------------------------------------------------------------- #


class TestSaveCookies:
    def test_uses_dpapi_and_writes_no_plaintext(
        self, isolated_data_dir: Path, session_with_secret: requests.Session
    ) -> None:
        assert login_mod.save_cookies(session_with_secret) == "dpapi"
        assert login_mod.LAST_SAVE_MODE == "dpapi"

        text = _cookie_file_text()
        doc = json.loads(text)
        assert doc["__weibo_enc__"] == 1
        assert doc["alg"] == "dpapi"
        assert SECRET not in text, "cookie 值泄漏到磁盘了"
        assert "SUBP" not in text, "cookie 名也应被整体加密"

    def test_falls_back_to_plaintext_when_dpapi_unavailable(
        self, isolated_data_dir: Path, session_with_secret: requests.Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DPAPI 不可用时退回旧格式，而不是拒绝登录。"""
        monkeypatch.setattr(login_mod, "dpapi_available", lambda: False)
        assert login_mod.save_cookies(session_with_secret) == "plaintext"
        doc = json.loads(_cookie_file_text())
        assert "cookies" in doc
        assert login_mod.load_cookies() is not None

    def test_save_failure_returns_failed_instead_of_raising(
        self, isolated_data_dir: Path, session_with_secret: requests.Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """保存失败只该警告——不能让一次成功的扫码登录白费。"""
        bad_dir = str(isolated_data_dir / "no-such" / "\x00invalid")
        monkeypatch.setattr(config, "DATA_DIR", bad_dir)
        monkeypatch.setattr(config, "COOKIE_FILE", bad_dir + "/cookies.json")
        assert login_mod.save_cookies(session_with_secret) == "failed"


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #


class TestLoadCookies:
    def test_missing_file_returns_none(self, isolated_data_dir: Path) -> None:
        assert login_mod.load_cookies() is None

    def test_restores_session(self, isolated_data_dir: Path, session_with_secret: requests.Session) -> None:
        login_mod.save_cookies(session_with_secret)
        restored = login_mod.load_cookies()
        assert restored is not None
        assert restored.cookies.get("SUB") == SECRET
        assert len(restored.cookies) == 3

    def test_plaintext_file_is_upgraded_in_place(self, isolated_data_dir: Path) -> None:
        """老用户手里的明文文件应自动升级，且登录态不受影响。"""
        plain = {
            "cookies": [
                {
                    "name": "SUB",
                    "value": SECRET,
                    "domain": ".weibo.com",
                    "path": "/",
                    "secure": False,
                    "expires": None,
                    "rest": {},
                    "rfc2109": False,
                }
            ]
        }
        Path(config.COOKIE_FILE).write_text(json.dumps(plain), encoding="utf-8")
        assert SECRET in _cookie_file_text()

        restored = login_mod.load_cookies()
        assert restored is not None
        assert restored.cookies.get("SUB") == SECRET
        assert SECRET not in _cookie_file_text(), "明文文件没有被就地升级"
        assert login_mod.load_cookies() is not None

    def test_returns_none_when_server_says_not_logged_in(
        self, isolated_data_dir: Path, session_with_secret: requests.Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        login_mod.save_cookies(session_with_secret)
        monkeypatch.setattr(login_mod, "_check_logged_in", lambda s: False)
        assert login_mod.load_cookies() is None

    def test_rate_limited_state_raises_login_error(
        self, isolated_data_dir: Path, session_with_secret: requests.Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """返回 None 表示「疑似风控」：登录态可能有效，不该当成未登录清掉。"""
        login_mod.save_cookies(session_with_secret)
        monkeypatch.setattr(login_mod, "_check_logged_in", lambda s: None)
        with pytest.raises(login_mod.LoginError, match="限流"):
            login_mod.load_cookies()


# --------------------------------------------------------------------------- #
# 坏文件的可读报错
# --------------------------------------------------------------------------- #


class TestCorruptFiles:
    def test_undecryptable_blob_gives_actionable_message(self, isolated_data_dir: Path) -> None:
        Path(config.COOKIE_FILE).write_text(
            json.dumps(
                {
                    "__weibo_enc__": 1,
                    "alg": "dpapi",
                    "data": base64.b64encode(b"definitely-not-a-valid-dpapi-blob").decode(),
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(login_mod.LoginError) as exc:
            login_mod.load_cookies()
        message = str(exc.value)
        assert "无法解密" in message
        assert "重新扫码" in message, "报错必须告诉用户下一步怎么做"

    def test_corrupt_base64_raises(self, isolated_data_dir: Path) -> None:
        Path(config.COOKIE_FILE).write_text(
            json.dumps(
                {
                    "__weibo_enc__": 1,
                    "alg": "dpapi",
                    "data": "!!!not base64!!!",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(login_mod.LoginError, match="损坏"):
            login_mod.load_cookies()

    def test_non_dict_document_raises(self, isolated_data_dir: Path) -> None:
        Path(config.COOKIE_FILE).write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(login_mod.LoginError, match="无法识别"):
            login_mod.load_cookies()

    def test_decrypted_payload_must_be_json(self, isolated_data_dir: Path) -> None:
        blob = login_mod.protect_bytes(b"this is not json at all")
        assert blob is not None
        Path(config.COOKIE_FILE).write_text(
            json.dumps(
                {
                    "__weibo_enc__": 1,
                    "alg": "dpapi",
                    "data": base64.b64encode(blob).decode(),
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(login_mod.LoginError, match="内容异常"):
            login_mod.load_cookies()
