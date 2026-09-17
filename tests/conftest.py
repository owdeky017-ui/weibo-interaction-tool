"""pytest 公共夹具。

1. 把工程根目录插进 ``sys.path``，测试文件可以直接 ``import config`` 等模块；
2. 提供 ``rec`` 记录工厂（实现在 ``_support.py``）；
3. 提供 ``isolated_data_dir``：把 ``config.DATA_DIR`` / ``COOKIE_FILE`` 指向
   临时目录。原 ``smoke_test.py`` 是手工保存/还原全局变量，漏还原就会把测试
   数据写进真实 ``data/``；改用 ``monkeypatch`` 后由 pytest 保证回滚。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PROJ = Path(__file__).resolve().parent.parent
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

import config  # noqa: E402  （必须在 sys.path 调整之后导入）
from _support import make_record  # noqa: E402


@pytest.fixture
def rec() -> Callable[..., dict[str, Any]]:
    """记录工厂，供各测试文件复用。"""
    return make_record


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 cookie / 输出目录整体挪到临时目录，避免污染真实 ``data/``。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "COOKIE_FILE", str(data_dir / "cookies.json"))
    return data_dir
