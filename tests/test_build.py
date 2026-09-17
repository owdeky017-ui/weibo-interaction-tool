"""``build.py`` 里写文件助手的不变量。

打包流程会两次改写仓库里的 ``build_mode.py``：开始前写入目标模式，
结束后还原原内容。两个函数都必须用 ``newline="\\n"`` 写盘 —— 否则在
Windows 上文本模式会把 ``\\n`` 翻译成 ``\\r\\n``，而仓库里所有 ``.py``
都是 LF（``.gitattributes`` 的 ``*.py text``），于是每次打包后
``git status`` 都会多出一个「内容没变、只有换行符变了」的假 diff。

这个坑已经复发两次（先是 ``BUILD_MODE_TEMPLATE`` 的引号与 coding 行
对不上，后来是 ``restore_build_mode`` 漏了 ``newline``），所以固定成用例。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import build


def test_repo_build_mode_is_lf() -> None:
    """仓库里的 ``build_mode.py`` 必须是 LF，与其它 ``.py`` 保持一致。"""
    raw = (Path(build.PROJ) / "build_mode.py").read_bytes()
    assert b"\r\n" not in raw, "build_mode.py 被写成了 CRLF"


def test_writers_preserve_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``write`` → ``restore`` 的往返必须逐字节还原，且不引入 CRLF。

    顺带固定另一条不变量：模板在 ``mode="self"`` 下的输出与仓库里的
    ``build_mode.py`` 完全相同 —— 两者一旦漂移，每次打包都会多一个假 diff。
    用例在临时目录里跑，不动仓库里的真实文件。
    """
    original = (Path(build.PROJ) / "build_mode.py").read_bytes()
    monkeypatch.setattr(build, "PROJ", str(tmp_path))
    target = tmp_path / "build_mode.py"
    target.write_bytes(original)

    build.write_build_mode("self")
    assert b"\r\n" not in target.read_bytes(), "write_build_mode 写入了 CRLF"
    assert target.read_bytes() == original, "模板输出与仓库里的 build_mode.py 不一致"

    build.restore_build_mode(build.read_build_mode())
    assert b"\r\n" not in target.read_bytes(), "restore_build_mode 写入了 CRLF"
    assert target.read_bytes() == original
