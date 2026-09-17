"""一键打包：按 A_MODE 生成可分发目录，并跑一遍三层自检。

产出与 `WeiboInteractionQuery - 副本` 保持完全一致的目录格式：

    <输出目录>\\
        WeiboInteractionQuery.exe
        _internal\\
        data\\            （运行时数据：cookies / 断点 / 导出结果）
        使用说明.txt

用法：
    python build.py                 # 打包 + 冻结环境自检
    python build.py --mode self     # 只打扫码A版
    python build.py --skip-probe    # 跳过打包后的冻结环境自检
    python build.py --probe         # 只跑冻结环境自检，不打包
    python build.py --clean         # 让 PyInstaller 清缓存重编（默认复用缓存，更快）
    python build.py --python X.exe  # 指定打包用的 Python 解释器

原理：同一份源码，打包前把 build_mode.A_MODE 写成目标值再编译，
      所以约束只有一个判定点，不会出现「改一处漏一处」。
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

PROJ = os.path.dirname(os.path.abspath(__file__))
BUILD_ROOT = os.path.join(PROJ, ".build")
APP_NAME = "WeiboInteractionQuery"

# 本机私有配置（不入库，见 build.local.example.json）。
# 用来覆盖下面的路径常量，避免把本机目录写进源码。
LOCAL_CONFIG = os.path.join(PROJ, "build.local.json")


def _local_config() -> dict:
    """读 build.local.json；缺失或格式不对时返回空 dict。"""
    try:
        with open(LOCAL_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_CFG = _local_config()


def _env_path(var: str, local_key: str, default: str = "") -> str:
    """按「环境变量 → build.local.json → 可移植默认值」的优先级取路径。

    本机目录一律不写死在源码里：需要指向别处时用环境变量或
    build.local.json 覆盖（见 README「环境与运行」）。
    """
    return os.environ.get(var) or _CFG.get(local_key) or default


def _detect_pack_python() -> str:
    """找一个装了 PyInstaller 的解释器。

    优先用 ``WEIBO_PACK_PY`` 或 build.local.json 的 ``pack_python``；
    否则依次试几个常见位置（项目内的 ``venv-pack/``、
    相邻检出的 ``weibo-interaction/venv-pack/``）。
    都没找到就返回空串，由 ``resolve_python()`` 回退到当前解释器 ——
    随后的依赖检查会给出明确报错，而不是在这里静默选错环境。
    """
    candidates = (
        os.path.join(PROJ, "venv-pack", "Scripts", "python.exe"),
        os.path.join(os.path.dirname(PROJ), "weibo-interaction", "venv-pack", "Scripts", "python.exe"),
    )
    for c in candidates:
        if os.path.exists(c):
            return c
    return ""


def _out_dir(mode: str) -> str:
    """某个模式的输出目录：环境变量 → 本地配置 → <项目>/dist/<mode>。"""
    local = _CFG.get("out") or {}
    return _env_path(
        f"WEIBO_OUT_{mode.upper()}", "", local.get(mode) if isinstance(local, dict) else ""
    ) or os.path.join(PROJ, "dist", mode)


# 参考目录：只用来对照结构，绝不修改。未设置或不存在时跳过对照。
REFERENCE_DIR: str = _env_path("WEIBO_REFERENCE_DIR", "reference_dir")

# 打包优先使用的解释器（含 PyInstaller / requests / openpyxl / Pillow）。
# 未设置或路径不存在时回退到当前解释器，见 resolve_python()。
DEFAULT_VENV_PY: str = os.environ.get("WEIBO_PACK_PY") or _CFG.get("pack_python") or _detect_pack_python()


BUILD_MODE_TEMPLATE = '''"""编译期常量 A_MODE。

    A_MODE = "self"    用户A 固定为当前扫码登录的账号（只填用户B）

**这个文件由 build.py 在打包前写入，不要手改。**
直接运行源码（python gui_app.py / python main.py）时用的就是这里当前的值，
本机需要改动时直接改这一行即可。
"""

A_MODE = "{mode}"
'''

README_SELF = """微博互动查询器 WeiboInteractionQuery · 使用说明（扫码A版）
=========================================================

【这是什么】
提取「你（当前扫码登录的账号）」与另一个微博用户之间的互动记录
（转发 / 评论 / 评论回复 / 点赞），一键导出 Excel 表格 + HTML 日志，
HTML 日志会自动在浏览器打开。

【用户A 是怎么确定的】
本版「用户A」固定为**当前扫码登录的微博账号**，不可手填（输入框是只读的）。
你只需要填写用户B（想查谁和「你」的互动）。

【第一步：扫码登录】
1. 点击「扫码登录微博」
2. 用微博 App 扫一扫二维码并确认
3. 登录态会自动保存，下次打开程序直接使用
4. 抓取中途登录失效时，程序会自动弹出新二维码，扫码后从断点继续

【第二步：填写用户】
· 用户A = 当前扫码登录的账号，登录成功后自动填入，不可修改。
  想查别人和你的互动，就用你自己的账号扫码登录。
· 用户B 支持三种写法：
  · 微博昵称（如：owdeky）
  · 主页链接（如：https://weibo.com/u/1234567890）
  · 数字 uid
提示：对方如果改过昵称，直接填 uid 或主页链接最稳。

【第三步：设置】
· 时间范围：最近 7 / 30 / 90 / 180 / 365 天，或选「自定义」输入起止日期
  （格式 YYYY-MM-DD，结束留空 = 到今天）
· 互动类型：勾选要查的互动（转发 / 评论 / 点赞）
· 抓取速度：慢最稳，快更省时间但容易触发微博风控（首次建议用「慢」）
· 导出格式：可只勾选需要的格式（HTML日志 / Excel / CSV），至少勾一个
· 所有配置会自动记忆，下次打开自动填好（用户A 不记忆，永远取登录账号）

【开始抓取】
1. 点「开始抓取」，耐心等待（微博数量多时较久，可看进度和预计剩余时间）
2. 期间可点「暂停 / 继续」随时暂停和恢复，点「结束」可提前收工并导出已扫部分
3. 抓取完成自动打开 HTML 日志，也可点「打开最新日志」或「打开结果文件夹」
4. 再次抓取同一对用户时自动增量刷新（只扫新增微博 + 重扫最近 3 天的旧微博）

【HTML 日志怎么用】
· 大标签页 = 方向（你→对方 / 对方→你），页内小标签页 = 转发 / 点赞 / 评论 / 评论回复
· 顶部统计卡片：总数 / 各类型 / 各方向 / 每月互动趋势
· 搜索框：输入关键词（内容 / 昵称 / 类型）实时过滤
· 类型下拉、起止日期：按类型或日期范围过滤
· 若顶部出现红色「有 N 条微博抓取失败」提示，说明部分数据可能缺失

【速度怎么选】
- 慢（最稳）：请求间隔 1.2 秒，几乎不会触发风控，适合大范围抓取
- 中：0.6 秒，日常推荐
- 快（易风控）：0.25 秒，小范围试试可以，大范围容易触发微博限制

【常见问题】
Q：为什么点赞很少或没有？
A：微博点赞列表接口不稳定，接口不可用时会自动暂停点赞扫描，每扫 50 条微博自动重试；结果可能不完整。
Q：为什么评论可能不全？
A：评论区存在折叠 / 拉黑隐藏机制，程序用「热度 + 时间」双排序尽量覆盖，极端情况下仍有遗漏。
Q：抓取中途断了怎么办？
A：程序自动保存断点，重新点「开始抓取」会从断点继续，不会重复扫描。
Q：提示「触发风控」？
A：程序会自动等待 180 秒后继续，不用管它；频繁触发就改用「慢」速度重跑。
Q：提示「登录态已失效」？
A：回到登录区重新扫码。注意：重新登录必须用**同一个账号**——用户A 就是登录账号，
   换了账号程序会中止并提示，避免把两个人的记录混在一起。
Q：用户B 填了自己？
A：程序会拒绝（查自己与自己的互动没有意义）。

【文件说明】
· WeiboInteractionQuery.exe：主程序（双击运行）
· _internal 文件夹：程序运行所需的库文件，请勿删除或移动
· data 文件夹：自动生成的登录态与结果文件（output 里是导出的日志）
· 结果文件名形如 微博互动_你的昵称_对方昵称_时间戳.xlsx / .csv / .html
· 本程序为绿色免安装，整个文件夹可随意拷贝；请勿只拷 exe 而不带 _internal

【关于账号】
程序使用你自己的微博账号登录去读取公开数据，仅供个人查看互动记录使用，
请勿用于商业用途或骚扰他人。

【打包信息】
Python 3.13 + PyInstaller，Windows 10/11 64 位。
"""


def _local_modes() -> dict:
    """从 build.local.json 的 "modes" 读本机构建目标（该文件不入库）。

    用来在不往仓库里放第二个产品变体的前提下，让本机仍能产出别的构建。
    每项形如 ``{"label":…, "desc":…, "out":…, "readme":…}``。
    """
    raw = _CFG.get("modes")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict) or not spec.get("out"):
            continue
        out[str(name)] = {
            "label": str(spec.get("label") or name),
            "desc": str(spec.get("desc") or ""),
            "out": str(spec["out"]),
            "readme": str(spec.get("readme") or ""),
        }
    return out


# 构建目标。对外只有扫码A版一个；额外的本机构建目标由 build.local.json 提供。
MODES = {
    "self": {
        "label": "扫码A版",
        "desc": "用户A 固定为当前扫码登录的账号，只需填用户B",
        "out": _out_dir("self"),
        "readme": README_SELF,
    },
}
MODES.update(_local_modes())


def log(msg):
    print(msg, flush=True)


def resolve_python(explicit=None):
    if explicit:
        return explicit
    if DEFAULT_VENV_PY and os.path.exists(DEFAULT_VENV_PY):
        return DEFAULT_VENV_PY
    return sys.executable


def write_build_mode(mode):
    """把 ``build_mode.A_MODE`` 写成目标值。

    ``newline="\\n"`` 不能省：默认的文本模式在 Windows 上会把 ``\\n``
    写成 ``\\r\\n``。配合下面 ``restore_build_mode`` 的字节级还原，
    打包不会在工作区留下任何改动。
    """
    path = os.path.join(PROJ, "build_mode.py")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(BUILD_MODE_TEMPLATE.format(mode=mode))
    return path


def read_build_mode():
    """读回 ``build_mode.py`` 的**原始字节**（不是文本）。

    必须是字节：工作区的换行符取决于 checkout 配置 —— ``core.autocrlf=true``
    的 Windows 环境检出就是 CRLF。按文本读会把 CRLF 归一成 LF，再写回去
    就等于把工作区文件换了换行符，``git status`` 于是多出一个
    「内容没变、只有换行符变了」的假 diff。
    """
    try:
        with open(os.path.join(PROJ, "build_mode.py"), "rb") as f:
            return f.read()
    except Exception:
        return None


def restore_build_mode(original):
    """把打包前的字节原样写回 —— 字节级还原，不做任何重新编码。"""
    if original is None:
        return
    try:
        with open(os.path.join(PROJ, "build_mode.py"), "wb") as f:
            f.write(original)
    except Exception:
        pass


def _fresh_dist(root, name):
    """给 PyInstaller 一个**空**的 dist 目录。

    PyInstaller 在 --noconfirm 下会先自己删掉 dist/<name>；文件上千个时既慢，
    又容易被安全软件/沙箱拦截（实测会直接报 SAFE_DELETE_BULK_CONFIRM_REQUIRED）。
    每次换一个没被占用的目录名，就完全不需要删除动作。
    """
    dist = os.path.join(root, "dist")
    if os.path.exists(os.path.join(dist, name)):
        dist = os.path.join(root, f"dist_{int(time.time())}")
    os.makedirs(dist, exist_ok=True)
    return dist


def run_pyinstaller(py, mode, clean=False):
    work = os.path.join(BUILD_ROOT, mode, "work")
    dist = _fresh_dist(os.path.join(BUILD_ROOT, mode), APP_NAME)
    os.makedirs(work, exist_ok=True)

    cmd = [py, "-m", "PyInstaller"]
    if clean:
        # 清掉分析缓存（会删 work 下上千个文件，故默认不开；改源文件后
        # PyInstaller 自己会按 mtime 重新分析，一般不需要）
        cmd.append("--clean")
    cmd += [
        "--noconfirm",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        dist,
        "--workpath",
        work,
        "--specpath",
        work,
        "--log-level",
        "WARN",
        # 二维码显示在窗口里需要 ImageTk（模块级 try/except 导入，显式声明更稳）
        "--hidden-import",
        "PIL.ImageTk",
        "--hidden-import",
        "PIL._tkinter_finder",
        "--hidden-import",
        "openpyxl",
        "--hidden-import",
        "et_xmlfile",
        # 优化版已移除 pandas/numpy，顺手排掉常见的大块头依赖
        "--exclude-module",
        "pandas",
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "matplotlib",
        "--exclude-module",
        "scipy",
        "--exclude-module",
        "IPython",
        "--exclude-module",
        "PyQt5",
        "--exclude-module",
        "PySide2",
        "--exclude-module",
        "pytest",
        os.path.join(PROJ, "gui_app.py"),
    ]

    log(f"  $ {' '.join(cmd[:6])} ...")
    t0 = time.time()
    p = subprocess.run(cmd, cwd=PROJ, capture_output=True, text=True, encoding="utf-8", errors="replace")
    dt = time.time() - t0
    if p.returncode != 0:
        log("  [失败] PyInstaller 返回码 " + str(p.returncode))
        log((p.stdout or "")[-4000:])
        log((p.stderr or "")[-4000:])
        raise SystemExit(1)
    warn = [line for line in (p.stderr or "").splitlines() if "WARNING" in line or "ERROR" in line]
    if warn:
        log(f"  （PyInstaller 警告 {len(warn)} 条，通常可忽略；仅显示前 3 条）")
        for line in warn[:3]:
            log("    " + line.strip())
    log(f"  PyInstaller 完成，用时 {dt:.0f} 秒")
    return os.path.join(dist, APP_NAME)


def assemble(built_dir, mode):
    target = MODES[mode]["out"]
    if os.path.exists(target):
        try:
            shutil.rmtree(target)
        except Exception as e:
            raise SystemExit(
                f"无法删除已存在的输出目录：{target}\n"
                f"  （{type(e).__name__}: {e}）\n"
                f"  请手动删掉这个文件夹后重跑，或改 build.py 里 MODES 的输出路径。\n"
                f"  注意：不要让我把新旧文件混在一起——那样会得到一个不完整的分发包。"
            ) from e
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copytree(built_dir, target)

    # data 子目录：与运行时一致（不携带任何登录态 / 历史结果）
    for sub in ("checkpoints", "output"):
        os.makedirs(os.path.join(target, "data", sub), exist_ok=True)

    readme = MODES[mode].get("readme") or ""
    with open(os.path.join(target, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(readme)
    return target


def read_embedded_mode(exe):
    """从 exe 内嵌 PYZ 里读出 build_mode 的编译常量。

    PyInstaller 会把纯 Python 模块编译进 exe 内嵌的 PYZ，
    所以 build_mode 的代码对象 co_consts 里留有 A_MODE 的字符串值。
    这是「不问程序本身」就能确认模式开关真的生效的唯一办法。
    """
    import marshal

    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception:
        return None
    try:
        a = CArchiveReader(exe)
        z = a.open_embedded_archive("PYZ.pyz")
        raw = z.extract("build_mode")
        if isinstance(raw, (bytes, bytearray)):
            code = None
            for off in (16, 0):  # 兼容带 / 不带 pyc 头
                try:
                    code = marshal.loads(raw[off:])
                    break
                except Exception:
                    code = None
            if code is None:
                return None
        else:
            code = raw
        for c in code.co_consts:
            if isinstance(c, str) and c in MODES:
                return c
    except Exception:
        return None
    return None


def verify(target, mode, mode_written_at):
    """结构自检：必须与参考目录同格式。"""
    ok = True
    exe = os.path.join(target, APP_NAME + ".exe")
    for name, path in (
        ("exe", exe),
        ("_internal/", os.path.join(target, "_internal")),
        ("data/", os.path.join(target, "data")),
        ("data/checkpoints/", os.path.join(target, "data", "checkpoints")),
        ("data/output/", os.path.join(target, "data", "output")),
        ("使用说明.txt", os.path.join(target, "使用说明.txt")),
    ):
        exists = os.path.exists(path)
        ok = ok and exists
        log(f"    {'[OK]' if exists else '[缺失]'} {name}")

    n_internal = sum(len(fs) for _, _, fs in os.walk(os.path.join(target, "_internal")))
    size_mb = (
        sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(target) for f in fs) / 1024 / 1024
    )
    log(f"    _internal 文件数 {n_internal}，整包 {size_mb:.1f} MB")

    # 关键：直接读 exe 内嵌 PYZ 里 build_mode 的编译常量
    embedded = read_embedded_mode(exe)
    if embedded is None:
        log("    [注意] 无法从 exe 读出内嵌模式（PyInstaller 版本差异），改用时间戳判断")
        fresh = os.path.getmtime(exe) >= mode_written_at - 1
        ok = ok and fresh
        log(f"    {'[OK]' if fresh else '[异常]'} exe 编译时间晚于 build_mode 写入时间")
    else:
        good = embedded == mode
        ok = ok and good
        log(f"    {'[OK]' if good else '[异常]'} exe 内嵌模式 = {embedded!r}（期望 {mode!r}）")

    # 不该携带任何运行时数据（登录态 / 历史结果）
    leftovers = []
    for r, _dirs, fs in os.walk(os.path.join(target, "data")):
        leftovers += [os.path.join(r, f) for f in fs]
    clean = not leftovers
    ok = ok and clean
    log(
        f"    {'[OK]' if clean else '[注意]'} data/ 未携带运行时数据"
        + ("" if clean else f"（发现 {len(leftovers)} 个文件）")
    )
    return ok, exe


def run_probe(py, clean=False):
    """把 probe_frozen.py 编成控制台 exe 并运行，验证冻结运行时依赖可用。

    与 gui_app 用同一套 PyInstaller 选项，只多一个「控制台模式」——
    否则 --windowed 会把结果吞掉。
    """
    work = os.path.join(BUILD_ROOT, "probe", "work")
    dist = _fresh_dist(os.path.join(BUILD_ROOT, "probe"), "probe_frozen")
    os.makedirs(work, exist_ok=True)

    cmd = [py, "-m", "PyInstaller"]
    if clean:
        cmd.append("--clean")
    cmd += [
        "--noconfirm",
        "--console",
        "--name",
        "probe_frozen",
        "--distpath",
        dist,
        "--workpath",
        work,
        "--specpath",
        work,
        "--log-level",
        "WARN",
        "--hidden-import",
        "PIL.ImageTk",
        "--hidden-import",
        "PIL._tkinter_finder",
        "--hidden-import",
        "openpyxl",
        "--hidden-import",
        "et_xmlfile",
        "--exclude-module",
        "pandas",
        "--exclude-module",
        "numpy",
        os.path.join(PROJ, "probe_frozen.py"),
    ]

    p = subprocess.run(cmd, cwd=PROJ, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        log("  [失败] 自检 exe 编译失败：")
        log((p.stdout or "")[-3000:])
        log((p.stderr or "")[-3000:])
        return False

    exe = os.path.join(dist, "probe_frozen", "probe_frozen.exe")
    log(f"  自检 exe：{exe}")
    r = subprocess.run(
        [exe],
        cwd=os.path.dirname(exe),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    log(r.stdout or "")
    if r.stderr:
        log("  --- stderr ---")
        log(r.stderr[-3000:])
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="打包可分发目录（--mode 选择 A_MODE）")
    ap.add_argument("--mode", choices=[*MODES, "all"], default="all", help="选择打包目标（默认 all）")
    ap.add_argument(
        "--clean", action="store_true", help="让 PyInstaller 清掉分析缓存后重编（默认复用缓存，更快）"
    )
    ap.add_argument("--python", default=None, help="打包用的 Python 解释器路径")
    ap.add_argument(
        "--probe", action="store_true", help="只跑「冻结环境自检」（编译 probe_frozen.py 并运行），不打包"
    )
    ap.add_argument("--skip-probe", action="store_true", help="打包完成后跳过冻结环境自检")
    args = ap.parse_args()

    py = resolve_python(args.python)
    log(f"工程目录：{PROJ}")
    log(f"打包解释器：{py}")
    if REFERENCE_DIR and os.path.exists(REFERENCE_DIR):
        log(f"结构参照：{REFERENCE_DIR}（只读，不会修改）")

    # 确认依赖齐全
    chk = subprocess.run(
        [py, "-c", "import PyInstaller, requests, openpyxl, PIL;print(PyInstaller.__version__)"],
        capture_output=True,
        text=True,
    )
    if chk.returncode != 0:
        log("[失败] 打包解释器缺少依赖（需要 PyInstaller / requests / openpyxl / Pillow）：")
        log(chk.stderr.strip())
        raise SystemExit(1)
    log(f"依赖检查通过（PyInstaller {chk.stdout.strip()}）")

    # ---------- 只跑自检 ----------
    if args.probe:
        log("")
        log("=" * 68)
        log("冻结环境自检（probe_frozen）")
        log("=" * 68)
        ok = run_probe(py, clean=args.clean)
        log("")
        log("自检结果：" + ("通过 ✔" if ok else "存在失败项，请看上面的输出 ✘"))
        raise SystemExit(0 if ok else 1)

    original = read_build_mode()
    modes = list(MODES) if args.mode == "all" else [args.mode]
    results = []
    built_exes = {}
    try:
        for mode in modes:
            info = MODES[mode]
            log("")
            log("=" * 68)
            log(f"打包 {info['label']} —— {info['desc']}")
            log(f"目标目录：{info['out']}")
            log("=" * 68)
            write_build_mode(mode)
            mode_written_at = os.path.getmtime(os.path.join(PROJ, "build_mode.py"))
            log(f"  已写入 build_mode.A_MODE = {mode!r}")
            built = run_pyinstaller(py, mode, clean=args.clean)
            target = assemble(built, mode)
            log(f"  已组装到：{target}")
            ok, exe = verify(target, mode, mode_written_at)
            results.append((info["label"], target, ok))
            built_exes[mode] = exe
    finally:
        restore_build_mode(original)

    # 交叉验证：不同目标的 exe 必须互不相同，否则说明 A_MODE 没写进去
    if len(built_exes) > 1:
        h = {}
        for m, p in built_exes.items():
            with open(p, "rb") as f:
                h[m] = hashlib.sha256(f.read()).hexdigest()[:16]
        same = len(set(h.values())) != len(h)
        log("")
        log("交叉验证：" + " / ".join(f"{m} exe {v}" for m, v in h.items()))
        if same:
            log("  [异常] 有 exe 完全相同 —— A_MODE 没有生效！")
            results = [(label, p, False) for label, p, _ in results]
        else:
            log("  [OK] 各目标 exe 互不相同，说明 A_MODE 确实改变了编译产物")

    log("")
    log("=" * 68)
    all_ok = all(ok for _, _, ok in results)
    log("打包完成" + ("" if all_ok else "（有自检项未通过，请查看上面的 [缺失]/[异常]）"))
    log("=" * 68)
    for label, path, ok in results:
        log(f"  {'[OK]' if ok else '[!] '} {label}：{path}")

    # ---------- 冻结环境自检（真跑一次打包后的运行时） ----------
    probe_ok = True
    if not args.skip_probe:
        log("")
        log("=" * 68)
        log("冻结环境自检（编译 probe_frozen 并运行）")
        log("=" * 68)
        probe_ok = run_probe(py, clean=args.clean)
        log("")
        log("自检结果：" + ("通过 ✔" if probe_ok else "存在失败项 ✘"))

    log("")
    log("提示：目录可直接双击 WeiboInteractionQuery.exe 运行；")
    log("      整个目录一起拷贝/压缩，不要只拷 exe。")
    log("      A_MODE 分支行为由 smoke_test.py 测试 7 覆盖。")
    if not all_ok or not probe_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
