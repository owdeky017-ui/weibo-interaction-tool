# 微博互动查询器

[English](./README.en.md) · **中文**

一个从零做起、一路做到打包分发的完整开发工程。完整历程：

| 阶段 | 时间 | 做了什么 |
|---|---|---|
| 起点 | 2026-08-15 | 想量化一个微博博主的说话习惯 → `urllib` 直连移动端接口 |
| 迭代 | 同一天 | 接口翻页拿不全 → 换 Selenium 驱动系统 Edge → 再换 Playwright，把翻页逻辑注入页面上下文执行，稳定拿到 1461 条微博 |
| 分析 | 同一天 | `analyze.py`：表情符号、语气词、标点习惯、句式、时间分布的统计 |
| 转向 | 之后 | 从「一个人发了什么」扩展到「两个人之间发生过什么」：转发、评论、评论回复、点赞 |
| 产品化 | 之后 | tkinter GUI + 扫码登录 + 一键导出，PyInstaller 打包成绿色免安装分发版 |
| 溯源 | 本月 | 源码散失，手上只剩打包产物 → 从 PYZ 读字节码，还原出 9 个模块的结构与 API 端点 |
| 工程化 | 本月 | 重建成完整源码工程：性能、登录态加密、三层验证、约束收敛成编译期常量（本仓库） |

本仓库是这条线的当前形态，抓取、分析、界面、打包、验证都在里面，clone 下来就能继续
开发，也能自己重新打包。早期那份 PyInstaller 分发包作为对照保留在本地磁盘上，
全程未被改动（MD5 已核对一致）。

产出物是一个 Windows 桌面工具：扫码登录后，查询「当前登录账号」与另一个微博用户
之间的全部互动记录（转发、评论、评论回复、点赞），一键导出 Excel、CSV 和 HTML 日志。

用户A 固定为当前扫码登录的账号，输入框只读，只需要填用户B。这是有意的设计约束，
理由见第六节。

> 不想装 Python？有现成的 Windows 免安装版：到
> [Releases](https://github.com/owdeky017-ui/weibo-interaction-tool/releases/latest)
> 下载 zip，解压后双击 `WeiboInteractionQuery.exe` 就能用，不需要 Python 环境，
> 也不需要联网装依赖。想读源码或自己打包看第八节。

---

## 一、性能

### 1. 转发 / 点赞「命中即停」

早期版本会先翻完所有页（转发最多 30 页 × 20 条，点赞最多 18 页 × 50 条），再判断
目标用户是否在其中。现在边翻边判，命中当页就返回。实测（见 `smoke_test.py` 测试 2）：

- 转发：目标在第 1 页 → 请求数 30 → 1
- 点赞：目标在第 2 页 → 请求数 18 → 2
- 目标不存在时仍翻完所有页，数据完整性不变

> 附带修正：早期版本的 `fetch_reposts` / `fetch_attitudes` 签名里的 `start_ts` 参数
> 从未被函数体使用（死参数），已移除并换成真正生效的 `target_uid`。

### 2. 评论不再无谓跑两遍

早期版本对时间序（`flow=1`）和热度序（`flow=0`）各翻最多 10 页。现在时间序若已把
接口声明的 `total_number` 条评论全部取回，说明两种排序看到的集合一致，直接跳过热度序。
只有在时间序被页数上限截断、拿到条数少于 `total_number`、或接口未返回
`total_number` 时才补跑，覆盖度不变。

### 3. 令牌桶限速（替代固定 `sleep`）

早期版本每次请求前 `sleep(min_interval)`，即使当前是并发场景也会串成一条直线。
改为令牌桶后，平均 QPS 与原来完全一致（仍是 1.2 / 0.6 / 0.25 秒一次），但允许额度内
的短突发，把并发请求的等待重叠掉。实测平均 QPS 不突破配置速率。

### 4. 扫描并发化（I/O 密集，不增加请求总数）

同一条微博需要拉四组数据：转发、评论、点赞、待审核评论，原来严格串行。现在用线程池
并发发起，`worker` 线程只做网络 I/O、不触碰共享状态，结果回到主线程统一处理。
并发度由 `config.SPEED_WORKERS` 控制：

| 速度 | 请求间隔 | 并发线程 |
|---|---|---|
| 慢（最稳） | 1.2 s | 1（严格串行） |
| 中 | 0.6 s | 2 |
| 快（易风控） | 0.25 s | 3 |

实测 3 个任务并发耗时 303 ms，串行 900 ms。`RiskControlError` 会被收集后在调用线程
统一上抛，外层「等待 180 秒后重试」的语义不变。

### 5. 断点改用 SQLite 增量写入

早期版本把整份断点放在一个 JSON 文件里，每次保存都要「读全量 → 按 `_key` 在内存合并
→ 写全量」，复杂度 O(记录数)。实测单个断点文件已到 665 KB。

改为 SQLite 后（`checkpoint.py`）：

- 去重交给主键 `INSERT OR IGNORE`，不再需要在内存里合并
- 保存只写还没落库的新增部分，复杂度 O(新增)
- 不再需要 `os.replace` 做原子替换，断电也不会把整份断点写坏
- 首次使用自动导入同名的旧 `.json` 断点，历史记录不丢，原文件保留不动

### 6. 移除 pandas / numpy

Excel 与 CSV 导出改用 `openpyxl` 加标准库 `csv`。打包体积减少约 19 MB
（pandas 13 MB + numpy 6 MB），冷启动更快。

> 附带发现：早期版本里对正文的 120 字截断是死代码。截断后的数据只用于汇总表，
> 不涉及正文，Excel/CSV 实际写的是完整记录。现在保持一致，保留全文。

---

## 二、健壮性 / 可维护性 / 安全

- 登录态改用 Windows DPAPI 加密。早期版本把含 `SUB`/`SUBP` 的登录态明文写在
  `data/cookies.json`，任何能读到该文件的人都能直接冒用账号。现在用
  `CryptProtectData` 加密，附加熵绑定到本程序；密文绑定当前 Windows 用户与机器，
  换机器解不开。旧的明文文件会被自动识别并就地升级，用户无感。
  实测 payload 区逐字节篡改全部被检测到。
- 断点保存失败不再静默。`_save_checkpoint` 原来是 `except Exception: pass`，
  写失败用户毫无感知，以为存了其实没存。现在会明确提示。
- A/B 两段扫描循环提取为一个方法 `_scan_batch`。原来 `run()` 里是两段复制粘贴的代码。
- `pause_event` / `stop_event` 在 `__init__` 中给出默认值。原先只在 `run()` 里赋值，
  导致 `_scan_weibo` / `_scan_comments` 无法脱离 `run()` 单独调用（写单测会 AttributeError）。
- 重新登录时的账号一致性校验。重新登录时若账号与开始时不一致，程序中止并提示。
  否则「用户A」变了，断点里的记录和 `scanned_mids` 就不再对应，会把两个人的记录
  混在一起。

---

## 三、「用户A 锁定」是怎么落地的

「用户A 只能取当前扫码登录的账号」这条硬约束没有散落在业务代码里，而是收敛成一个
编译期常量 `build_mode.A_MODE`，由 `build.py` 在打包前写入 `build_mode.py` 再编译。
约束因此只有一个判定点，改需求时不会漏掉某个分支。

| 分支点 | 本版行为 |
|---|---|
| GUI 用户区标题 | 「2. 选择要对比的用户」 |
| 用户A 输入框 | 只读，登录后自动填入 |
| GUI 配置记忆 | 只记忆 B（A 永远取登录账号） |
| GUI `_start` 校验 | 只校验 B，且 B 不能是登录账号本人 |
| CLI `--u1` | 隐藏（`argparse.SUPPRESS`），传了会明确报错 |
| CLI 交互引导 | 只询问 B |
| 登录账号 uid | 就是用户A 本人（必然非空） |
| 登录失效重登 | 校验账号一致，不一致则中止 |

最后一条关系到数据正确性。「用户A = 登录账号」这个约束下，如果重新扫码时换了账号，
A 就变了，断点里的历史记录和 `scanned_mids` 都不再对应新的用户对，继续跑会把两个人
的记录混在一起。所以程序必须中止，不能续传。

把常量收敛出来还有可测试性上的好处：测试可以在同一进程里替换 `build_mode` 再重新
导入模块，让各个取值都被跑到。测试是真实构建 GUI 后读控件状态，不只断言常量
（`smoke_test.py` 测试 7，见第四节）。

---

## 四、验证：三层检查

每层回答一个不同的问题。

### 第 1 层：源码行为（`smoke_test.py`，119 项断言）

```powershell
python smoke_test.py
```

| 测试 | 覆盖内容 |
|---|---|
| 1 | exporter 去 pandas 后的 Excel / CSV / HTML 产出（sheet 顺序、表头、长文本不被截断、空记录分支、汇总透视表） |
| 2 | 转发 / 点赞「命中即停」：命中页数、目标不存在时不提前退出 |
| 3 | 评论双排序的跳过条件（`has_more` + `total_number` 三种场景） |
| 4 | SQLite 增量断点：`.json`→`.db` 改写、增量追加、主键去重、`_key=None` 不去重、旧 JSON 迁移、损坏文件容错 |
| 5 | 令牌桶：突发额度、平均速率、不突破配置 QPS |
| 6 | `_run_tasks` 并发、单任务走串行分支、`RiskControlError` 在调用线程上抛 |
| 7 | 约束分支：CLI 参数解析 + 真实构建 GUI 后读控件状态（`importlib.reload` 换掉 `build_mode`） |
| 8 | 登录态加密：加解密往返、逐字节篡改检测、明文文件就地升级、解密失败报错、DPAPI 不可用时退回明文 |

另有 `pytest` 用例集（`tests/`，210 项），覆盖 client / analyzer / exporter /
checkpoint / login / fetchers / utils 的边界条件，CI 上跑覆盖率。

### 第 2 层：打包产物结构（`build.py` 内建自检）

每次打包自动执行，输出 `[OK]/[缺失]/[异常]`：

- 结构完整（`exe` / `_internal/` / `data/checkpoints/` / `data/output/` / `使用说明.txt`）
- 直接读 exe 内嵌 PYZ 里 `build_mode` 的编译常量，确认等于目标值
- `data/` 未携带任何运行时数据（不会把你的登录态打进分发包）
- 产出 exe 的 sha256 与预期一致

### 第 3 层：冻结运行时（`python build.py --probe`）

单独跑，也可在打包后自动跑，加 `--skip-probe` 可跳过：

```powershell
python build.py --probe
```

把 `probe_frozen.py` 用与 gui_app 相同的 PyInstaller 选项编成控制台 exe 并运行，
回答「打包后这些真的能用吗」。PyInstaller 只能证明文件在，证明不了运行时可用：

```
[OK  ] 导入全部依赖模块
        —— requests 2.34.2 / openpyxl 3.1.5 / sqlite3 3.50.4 / tk 8.6 / PIL 12.3.0
[OK  ] HTTPS 请求 + CA 证书链
        —— HTTP 302，419 字节（SSL 校验通过）
[OK  ] SQLite 断点读写
[OK  ] Excel / CSV / HTML 导出
[OK  ] tkinter + PIL.ImageTk
[OK  ] frozen 路径解析（BASE_DIR = exe 目录）
6 通过 / 0 失败
```

> 这一层专门用来抓「证书链缺 `cacert.pem`」「`sqlite3.dll` 版本不对」
> 「`PIL.ImageTk` 找不到 `_imaging`」这类只有真跑一次才暴露的问题。

另有两个独立脚本，可随时复查：

```powershell
python verify_package.py     # 对已打包目录，读 exe 内嵌 PYZ 里的 build_mode 常量
#   [OK] 扫码A版：exe 内 build_mode = 'self'（期望 'self'）

python verify_assemble.py    # 复用 .build 里已编译的产物，走一遍「组装 + 自检」链路
#   [OK] exe 内嵌模式 = 'self'（期望 'self'）
#   [OK] 使用说明.txt 与目标定义一致
#   [OK] data/ 内容 = ['checkpoints', 'output']
```

---

## 五、踩过的坑

都是「写的时候没想到、跑起来才炸」的问题，单独记一笔。

### 1. PyInstaller 二次打包会失败：`SAFE_DELETE_BULK_CONFIRM_REQUIRED`

`build.py` 第一次跑得好好的，第二次直接报错。原因是 `--noconfirm` 下 PyInstaller
会先删掉已存在的 `dist/<name>`，而那个目录里有上千个文件，触发了「批量删除需要确认」
的保护。

修法是绕开删除动作本身：每次打包输出到全新的 `dist_<时间戳>` 目录，组装完再移动。
`--clean` 也从默认改为可选，清缓存只会拖慢下次打包，不该是默认行为。

### 2. 冻结环境的中文输出乱码

`probe_frozen.py` 编成 exe 后输出全是乱码。原因不在 PyInstaller：冻结运行时下
stdout 走的是系统区域编码（中文 Windows 是 GBK），而调用方按 UTF-8 读。修法是脚本
启动时强制 `sys.stdout.reconfigure(encoding="utf-8")`。这个坑只在「父进程读子进程
stdout」时出现，本地直接跑看不到。

### 3. `_internal/` 里看不到 urllib3，不代表没打进去

第一次检查打包结果时，我在 `_internal/` 里没找到 `urllib3`、`idna`、
`PIL/ImageTk.py`，一度判断为「依赖缺失」。

实际上这是正常的：纯 Python 模块被编译进 exe 内嵌的 PYZ（本例 729 个模块），
只有带 `.pyd` 扩展的包才会以目录形式落在 `_internal/`。要确认是否齐全，得展开 PYZ
去数模块，不能看目录。

顺带确认了 `pandas` / `numpy` 确实没被混进去。这正是移除这两个依赖的目的。

### 4. DPAPI 的完整性保护有边界

登录态加密用 Windows DPAPI 之后，我做了一次逐字节篡改测试：payload 区 274 字节全部
被检测到。但 DPAPI blob 的字节 4 到 19 不受完整性保护，那是 `dwFlags` 加 16 字节
description 字段，属 API 的已知行为，不涉及密文内容。不实测一次的话，很容易误以为
整块 blob 都不可篡改，从而写出错误的威胁模型。

### 5. 打包产物「能启动」不等于「能运行」

最初的验证只做到「exe 起来后存活 12 秒不崩」。这只能证明启动路径没炸，证明不了
运行时可用：证书链缺 `cacert.pem`、`sqlite3.dll` 版本不对、`PIL.ImageTk` 找不到
`_imaging`，这三类问题都要真跑一次才暴露。这正是第四节第 3 层存在的理由。

### 6. CI 从没真正在 GitHub 上跑过，第一次跑就红了两次

仓库初始化时漏提交了 `models.py`、`tests/`、`.github/workflows/ci.yml` 三块内容，
clone 下来既没有 CI 定义，也跑不了 pytest。

补齐后推上去，CI 第一次真正执行，接连暴露两个问题：

1. `ruff check .` 在 `smoke_test.py` 上有 87 项问题（补签名注解、`open()` 改
   `with`、列表拼接改解包、超长行折行）。
2. 更隐蔽的一个：`mypy` 在本机 Windows 上过，在 CI 的 Linux runner 上不过。
   `login.py` 用了 `ctypes.windll`、`gui_app.py` 用了 `os.startfile`，这些符号在
   Linux 的 typeshed 里不存在；而 `if sys.platform != "win32"` 之后的代码在 Linux
   视角下会被判成 unreachable。mypy 默认按运行它的主机选类型桩，于是同一份代码、
   两台机器、两种结论。解法是在 `pyproject.toml` 里显式钉住目标平台
   （`platform = "win32"`），让检查结果与主机无关。

判断依据应该换成「新机器 clone 下来能不能跑」。

---

## 六、安全与边界：这个工具不做什么

「扫码A版」把用户A 锁死为当前登录账号，是有意的设计约束：

- 它天然只能查「与你有关」的互动。想查两个都跟你无关的账号，做不到。
- 抓到的数据不出本机：没有上传、没有服务端、没有账号体系。
- 登录态加密存在本机 `data/` 下，且该目录已在 `.gitignore` 中排除。

为什么不做成在线服务：把「扫码登录 + 抓取」搬成公开网站在技术上完全可行，
但会让服务器保存访客的微博登录态（形态上与钓鱼站无异）、违反微博用户协议，
且公开提供「查某人和某人的互动」本身就是隐私问题。所以这个项目保持为本地桌面工具。

---

## 七、项目展示站

`site/` 是一个静态展示站，讲清架构、性能实测数据与验证方法，零外链资源
（可离线双击打开）。用来看成果或当简历附件都行。

```powershell
python make_sample_report.py      # 重新生成样例报告
python -m http.server 8766 --directory site    # 本地预览
```

`site/sample/sample-report.html` 是用真实的 `exporter.export()` 生成的，只把输入换成
合成数据（52 条记录、跨 7 个月、覆盖全部类型与两个方向），所以它的排版、筛选、统计
逻辑和用户自己跑出来的完全一致。

---

## 八、环境与运行

依赖按用途分组（`pyproject.toml` 的 `optional-dependencies`）：

| 组 | 内容 | 用途 |
|---|---|---|
| 核心 | `requests` / `openpyxl` | 抓取与导出 |
| `gui` | `Pillow` | 窗口里显示登录二维码 |
| `dev` | `ruff` / `mypy` / `pytest` / `pytest-cov` | 静态检查与测试 |
| `build` | `pyinstaller` | 打包 |

```powershell
pip install -e ".[dev,gui]"     # 开发环境
pip install -e ".[build]"       # 打包环境（可另建一个 venv，互不污染）
```

源码直跑（行为由 `build_mode.py` 当前值决定）：

```powershell
python gui_app.py                        # 图形界面
python main.py --u2 <用户B> --days 30    # 命令行
```

跑测试与静态检查：

```powershell
python smoke_test.py    # 119 项断言：导出 / 剪枝 / 断点 / 限速 / 并发 / 约束分支 / 加密
pytest                  # 210 项用例（tests/）
ruff check .            # lint
ruff format --check .   # 格式
mypy                    # 类型检查（检查目标写在 pyproject.toml 的 [tool.mypy] files 里）
```

打包与产物复查：

```powershell
python build.py               # 打包 + 组装 + 三层自检（默认复用 PyInstaller 缓存）
python build.py --mode self   # 只打扫码A版
python build.py --clean       # 清掉分析缓存后重编
python build.py --probe       # 只跑冻结运行时自检，不打包
python verify_package.py      # 读 exe 内嵌 PYZ 里的 build_mode 常量（需打包环境）
```

`build.py` 里的本机目录（打包解释器、结构参照目录、输出目录）不写死在源码里，
按「环境变量 → `build.local.json` → 可移植默认值」三级取值：

| 环境变量 | 配置文件字段 | 默认值 |
|---|---|---|
| `WEIBO_PACK_PY` | `pack_python` | 自动探测 `venv-pack/`，找不到则回退当前解释器 |
| `WEIBO_REFERENCE_DIR` | `reference_dir` | 空（跳过结构对照） |
| `WEIBO_OUT_<MODE>` | `out.<mode>` | `<项目>/dist/<mode>` |

复制 `build.local.example.json` 为 `build.local.json` 即可覆盖；后者不入库。
需要在本机额外定义构建目标时，可在该文件里加 `modes.<名称>`
（`label` / `desc` / `out` / `readme` 四项），`--mode` 的候选会自动带上它。

> `smoke_test.py` 在 Tk 起不来时（无图形会话、tcl 资源读不到）只跳过 GUI 那一段并打印
> `[SKIP]`，不会把环境问题误报成失败，与 `tests/` 里 `tk_root` 夹具的语义保持一致。
> `requirements.txt` 是打包环境的运行时依赖清单，内容与上表「核心 + `gui`」一致。

---

## 九、目录结构

```
weibo-interaction-opt/
├── README.md         # 本文件（中文）
├── README.en.md      # 英文版
├── LICENSE           # MIT
├── build.py          # 一键打包（写 A_MODE → PyInstaller → 组装目录 → 三层自检）
├── build.local.example.json  # 本机路径覆盖模板（复制为 build.local.json，后者不入库）
├── build_mode.py     # 编译期常量 A_MODE（由 build.py 写入，勿手改）
├── probe_frozen.py   # 冻结环境自检脚本（被 build.py --probe 编译并运行）
├── verify_package.py # 复查已打包目录内嵌的 build_mode 值
├── verify_assemble.py# 复用已编译产物，走一遍「组装 + 自检」链路
├── smoke_test.py     # 冒烟测试（119 项断言，单文件线性脚本）
├── tests/            # pytest 用例集（210 项）
│   ├── conftest.py
│   ├── _support.py
│   └── test_*.py     # client / analyzer / exporter / checkpoint / login / fetchers / utils / build / build_modes
├── .github/workflows/ci.yml   # CI：ruff + mypy（Linux）· pytest + 覆盖率（Windows）
├── gui_app.py        # 图形界面入口
├── main.py           # 命令行入口
├── login.py          # 扫码登录 + DPAPI 加密持久化 + 手动 cookie
├── client.py         # API 客户端（令牌桶限速 / 每线程 Session / 数据接口）
├── fetchers.py       # 微博条目解析、@提及提取
├── analyzer.py       # 互动识别与聚合（并发扫描 + 断点续传）
├── checkpoint.py     # 断点存储（SQLite 增量写入 + 旧 JSON 自动迁移）
├── exporter.py       # Excel/CSV/HTML 导出（openpyxl + csv，无 pandas）
├── models.py         # 数据结构定义（WeiboItem / CommentItem / InteractionRecord 等）
├── server.py         # 本地日志刷新服务（可选，不打包进 exe）
├── utils.py          # 微博时间解析
├── config.py         # 配置
├── 使用说明.txt       # 源码目录的说明（打包时由 build.py 按目标重新生成）
├── make_sample_report.py  # 生成展示站用的样例报告（走真实 exporter，输入为合成数据）
├── site/             # 项目展示站（静态，零外链资源）
│   ├── index.html
│   └── sample/sample-report.html
├── .build/           # PyInstaller 中间产物（可删，删了下次打包变慢）
└── data/             # cookies、断点、输出（运行时生成，不入库）
```

---

## 十、说明与限制

1. 点赞：微博网页版没有公开的点赞列表接口，程序改用移动端接口尽力尝试，接口不可用
   时自动暂停并在每 50 条微博后重试。
2. @提及：从微博文本中解析 `@` 链接与文本，按 uid 精确匹配、昵称归一化匹配。
3. 评论楼中楼：默认深度扫描每条评论下的回复；评论量大时明显变慢，可用 `--no-replies`
   关闭。
4. 风控：微博对非登录高频请求有风控。触发时程序自动等待 180 秒重试；频繁触发请降到
   「慢」速度。
5. 可见性：只能抓到公开微博及其公开互动；对方隐私设置或已删除内容无法获取。
6. 微博时间均为北京时间（东八区）。
