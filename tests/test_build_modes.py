"""A_MODE 分支测试：``manual`` / ``self`` 两种取值都覆盖到。

``build.py`` 在打包前改写 ``build_mode.A_MODE``，再打包出对应目录。
同一份代码带两个取值，风险在于分支会随时间腐化——某个新加的
提示文案或控件只在其中一个取值下生效。这里把各个分叉点全部固定下来。

实现方式：伪造 ``build_mode`` 模块后重新导入 ``main`` / ``gui_app``，
再断言 CLI 参数与 GUI 控件状态。模块级 ``skipif`` 保证无 tkinter 环境跳过。
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator
from typing import Any

import pytest

try:
    import tkinter as tk

    HAS_TK = True
except Exception:  # pragma: no cover - 取决于环境
    HAS_TK = False

pytestmark = pytest.mark.skipif(not HAS_TK, reason="需要 tkinter（Windows 自带）")

_MODULES = ("build_mode", "main", "gui_app")


@pytest.fixture
def tk_root() -> Iterator[tk.Tk]:
    """隐藏的根窗口；无图形环境时跳过而不是报错。"""
    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"无法创建 Tk 窗口：{exc}")
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


@pytest.fixture
def load_mode() -> Iterator[Callable[[str], tuple[Any, Any]]]:
    """``load_mode(mode) -> (main, gui_app)``；用例结束后恢复真实模块。"""
    saved = {name: sys.modules.get(name) for name in _MODULES}

    def _load(mode: str) -> tuple[Any, Any]:
        fake = types.ModuleType("build_mode")
        fake.A_MODE = mode
        sys.modules["build_mode"] = fake
        for name in ("main", "gui_app"):
            sys.modules.pop(name, None)
        import gui_app
        import main

        return main, gui_app

    yield _load

    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


# --------------------------------------------------------------------------- #
# self 版：用户A = 扫码登录账号
# --------------------------------------------------------------------------- #


class TestSelfMode:
    def test_self_is_a_flag(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        main, gui = load_mode("self")
        assert main.SELF_IS_A is True
        assert gui.SELF_IS_A is True

    def test_u1_is_hidden_and_unset(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        main, _ = load_mode("self")
        assert main.parse_args(["--u2", "1234567890"]).u1 is None

    def test_u2_parses_normally(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        main, _ = load_mode("self")
        assert main.parse_args(["--u2", "1234567890"]).u2 == "1234567890"

    def test_explicit_u1_is_rejected(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        main, _ = load_mode("self")
        with pytest.raises(SystemExit) as exc:
            main.main(["--u1", "1", "--u2", "2"])
        assert "不再支持 --u1" in str(exc.value)

    def test_help_mentions_locked_user_a(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        _, gui = load_mode("self")
        assert "不可修改" in gui.HELP_TEXT
        assert "用户A = 当前扫码登录的账号" in gui.HELP_TEXT

    def test_help_mentions_relogin_consistency(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        """重新登录换账号会让「用户A」悄悄变人，必须提醒用户。"""
        _, gui = load_mode("self")
        assert "重新登录的账号必须和开始时一致" in gui.HELP_TEXT


# --------------------------------------------------------------------------- #
# A_MODE = "manual"：A / B 都手填
# --------------------------------------------------------------------------- #


class TestManualMode:
    def test_self_is_a_flag(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        main, gui = load_mode("manual")
        assert main.SELF_IS_A is False
        assert gui.SELF_IS_A is False

    def test_u1_parses_normally(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        main, _ = load_mode("manual")
        assert main.parse_args(["--u1", "111", "--u2", "222"]).u1 == "111"

    def test_missing_u1_raises(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        main, _ = load_mode("manual")
        with pytest.raises(SystemExit) as exc:
            main.main(["--u2", "222"])
        assert "缺少用户A" in str(exc.value)

    def test_help_lists_three_input_forms(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        _, gui = load_mode("manual")
        assert "用户A / 用户B 支持三种写法" in gui.HELP_TEXT

    def test_help_has_no_self_only_wording(self, load_mode: Callable[[str], tuple[Any, Any]]) -> None:
        """A_MODE="self" 专属文案不能泄漏到另一分支。"""
        _, gui = load_mode("manual")
        assert "不可修改" not in gui.HELP_TEXT
        assert "重新登录的账号必须和开始时一致" not in gui.HELP_TEXT


# --------------------------------------------------------------------------- #
# GUI 控件状态
# --------------------------------------------------------------------------- #


class TestGuiWidgets:
    def test_self_mode_user_a_is_readonly(
        self, load_mode: Callable[[str], tuple[Any, Any]], isolated_data_dir: Any, tk_root: tk.Tk
    ) -> None:
        _, gui = load_mode("self")
        app = gui.App(tk_root)
        assert str(app.entry_a.cget("state")) == "readonly"
        assert app.a_var is not None
        assert app.a_var.get() == "（未登录）"
        assert "选择要对比的用户" in str(app.entry_a.master.cget("text"))

    def test_manual_mode_user_a_is_editable(
        self, load_mode: Callable[[str], tuple[Any, Any]], isolated_data_dir: Any, tk_root: tk.Tk
    ) -> None:
        _, gui = load_mode("manual")
        app = gui.App(tk_root)
        assert str(app.entry_a.cget("state")) == "normal"
        assert app.a_var is None
        assert "填写两个微博用户" in str(app.entry_a.master.cget("text"))

    def test_self_mode_shows_login_hint(
        self, load_mode: Callable[[str], tuple[Any, Any]], isolated_data_dir: Any, tk_root: tk.Tk
    ) -> None:
        """self 版必须在界面上说明「用户A 不可修改」，否则用户会以为输入框坏了。"""
        _, gui = load_mode("self")
        app = gui.App(tk_root)
        labels = [w.cget("text") for w in app.entry_a.master.winfo_children() if w.winfo_class() == "TLabel"]
        assert any("不可修改" in str(t) for t in labels), labels


# --------------------------------------------------------------------------- #
# 真实 build_mode.py
# --------------------------------------------------------------------------- #


class TestBuildModeFile:
    def test_default_value_is_valid(self) -> None:
        """仓库里的默认值必须是两个合法模式之一（打包时由 build.py 覆盖）。"""
        sys.modules.pop("build_mode", None)
        import build_mode

        assert build_mode.A_MODE in ("self", "manual")
