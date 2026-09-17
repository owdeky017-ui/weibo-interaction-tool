"""端到端验证 build.py 的「组装 + 自检」链路（不重新编译）。

复用 .build/<mode>/dist 里已有的 PyInstaller 产物，把它当成刚编译完的结果，
走一遍 assemble() → verify()，确认：
  · 目录结构正确
  · 能从 exe 内嵌 PYZ 读出 build_mode 编译常量
  · data/ 干净
  · 使用说明.txt 与目标定义一致
输出到一个临时目录，不碰正式交付目录。
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build as B

ok_all = True
for mode in B.MODES:
    built = os.path.join(B.BUILD_ROOT, mode, "dist", B.APP_NAME)
    if not os.path.isdir(built):
        print(f"[跳过] {mode}：找不到已编译产物 {built}")
        continue

    scratch = tempfile.mkdtemp(prefix=f"verify_{mode}_")
    out = os.path.join(scratch, "WeiboInteractionQuery")
    B.MODES[mode]["out"] = out  # 只在内存里改，不写回文件
    try:
        B.assemble(built, mode)
        print(f"\n=== {mode} ===")
        ok, exe = B.verify(out, mode, mode_written_at=0)
        ok_all = ok_all and ok

        # 使用说明.txt 必须是该目标对应的那一份
        with open(os.path.join(out, "使用说明.txt"), encoding="utf-8") as f:
            txt = f.read()
        good = txt == (B.MODES[mode].get("readme") or "")
        ok_all = ok_all and good
        print(f"    {'[OK]' if good else '[异常]'} 使用说明.txt 与目标定义一致")

        # data/ 必须只有两个空子目录
        entries = sorted(os.listdir(os.path.join(out, "data")))
        good = entries == ["checkpoints", "output"]
        ok_all = ok_all and good
        print(f"    {'[OK]' if good else '[异常]'} data/ 内容 = {entries}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

print()
print("端到端验证：" + ("全部通过 ✔" if ok_all else "存在异常 ✘"))
sys.exit(0 if ok_all else 1)
