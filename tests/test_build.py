"""``build.py`` 里写文件助手的不变量。

打包流程会两次改写仓库里的 ``build_mode.py``：开始前 ``write_build_mode``
写入目标模式、结束后 ``restore_build_mode`` 还原原内容。这里固定两条：

1. ``write_build_mode`` 永远写 LF（不随平台变）；
2. ``read_build_mode`` / ``restore_build_mode`` 是**字节级**的 —— 还原后
   与打包前逐字节相同。

第 2 条不是洁癖：工作区的换行符取决于 checkout 配置（``core.autocrlf=true``
的 Windows 环境检出就是 CRLF）。按文本读、按文本写会把 CRLF 归一成 LF，
于是 ``git status`` 多出一个「内容没变、只有换行符变了」的假 diff。
这个坑已经复发两次（先是 ``BUILD_MODE_TEMPLATE`` 的引号与 coding 行对不上，
后来是还原时换了换行符），所以固定成用例。

**不要断言「工作区的 build_mode.py 是 LF」** —— 那是 checkout 的产物，
在 CI 的 Windows runner 上就是 CRLF，拿它当断言会直接红（踩过一次）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import build

# 用 __file__ 定位，不依赖 build.PROJ（用例会把它指向临时目录）
REPO_FILE = Path(__file__).resolve().parent.parent / "build_mode.py"


def _as_lf(raw: bytes) -> bytes:
    """把 CRLF 归一成 LF（工作区文件的换行符取决于 checkout 配置）。"""
    return raw.replace(b"\r\n", b"\n")


def test_write_emits_lf_and_matches_repo_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``write_build_mode`` 必须写 LF，且内容与仓库里的 ``build_mode.py`` 一致。"""
    monkeypatch.setattr(build, "PROJ", str(tmp_path))
    target = tmp_path / "build_mode.py"

    build.write_build_mode("self")
    written = target.read_bytes()

    assert b"\r\n" not in written, "write_build_mode 写入了 CRLF"
    assert written == _as_lf(REPO_FILE.read_bytes()), "模板输出与仓库里的 build_mode.py 不一致"


@pytest.mark.parametrize("original", [b"A_MODE = 'self'\r\n", b"A_MODE = 'self'\n"])
def test_restore_is_byte_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: bytes) -> None:
    """``restore_build_mode`` 必须逐字节还原，换行符也要与打包前一致。"""
    monkeypatch.setattr(build, "PROJ", str(tmp_path))
    target = tmp_path / "build_mode.py"
    target.write_bytes(original)

    saved = build.read_build_mode()
    assert saved == original, "read_build_mode 没有按字节读"

    build.write_build_mode("manual")
    assert target.read_bytes() != original, "write_build_mode 没有改写文件"

    build.restore_build_mode(saved)
    assert target.read_bytes() == original, "restore_build_mode 没有逐字节还原"
