"""校验打包后的 exe 里 build_mode 编译常量确实是目标值。

原理：PyInstaller 把纯 Python 模块编译进 exe 内嵌的 PYZ，
      所以 build_mode 的代码对象 ``co_consts`` 里会留下该常量的字符串值。
      这是唯一能「不问程序本身」就确认 A_MODE 已生效的办法 ——
      比对时间戳或文件哈希只能推断，这个是直接读编译产物。

用法（本地校验脚本，不带参数时校验内置目标的产物）::

    python verify_package.py                  # 校验内置目标的输出目录
    python verify_package.py <exe> <mode>     # 校验指定的产物
"""

import marshal
import os
import sys

from PyInstaller.archive.readers import CArchiveReader

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build as _build  # 要先补 sys.path 才能导入同目录模块

# 默认校验内置目标：输出目录与程序名都从 build.MODES 派生，
# 不写死本机路径（build.local.json 覆盖后自然跟着走）。
DEFAULT_LABEL = _build.MODES["self"]["label"]
DEFAULT_EXE = os.path.join(_build.MODES["self"]["out"], _build.APP_NAME + ".exe")
DEFAULT_EXPECT = "self"

# 合法取值 = 构建目标的键（见 build.py 的 MODES）
KNOWN_MODES = tuple(_build.MODES)


def read_embedded_mode(exe: str) -> str | None:
    """从 exe 内嵌 PYZ 里读出 ``build_mode`` 模块的编译常量。

    PYZ 条目前面带一段 header，不同 PyInstaller 版本偏移不一致，
    所以依次试 16 / 0 两个偏移，再退回整体反序列化。
    """
    archive = CArchiveReader(exe)
    pyz = archive.open_embedded_archive("PYZ.pyz")
    raw = pyz.extract("build_mode")

    if not isinstance(raw, (bytes, bytearray)):
        code = raw
    else:
        code = None
        for offset in (16, 0):
            try:
                code = marshal.loads(raw[offset:])
                break
            except Exception:
                code = None
        if code is None:
            code = marshal.loads(raw)

    modes = [c for c in code.co_consts if isinstance(c, str) and c in KNOWN_MODES]
    return modes[0] if modes else None


def main(argv: list[str]) -> int:
    label = DEFAULT_LABEL
    exe = DEFAULT_EXE
    expect = DEFAULT_EXPECT
    if len(argv) >= 2:
        exe, expect = argv[0], argv[1]
        label = argv[2] if len(argv) > 2 else exe

    got = read_embedded_mode(exe)
    good = got == expect
    print(f"  [{'OK' if good else '异常'}] {label}：exe 内 build_mode = {got!r}（期望 {expect!r}）")
    print()
    print("结论：" + ("A_MODE 已在编译产物中生效。" if good else "不匹配，需要重新打包！"))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
