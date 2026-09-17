"""全局配置：路径、请求参数、抓取开关。

本模块只放常量，不做任何 I/O。所有路径都基于 ``BASE_DIR`` 派生：
打包成 exe 后 ``BASE_DIR`` 是 exe 所在目录，源码运行时是脚本目录。
"""

from __future__ import annotations

import os
import sys

# 项目根目录：打包成 exe 后取 exe 所在目录，否则取脚本目录
if getattr(sys, "frozen", False):
    BASE_DIR: str = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 运行时数据目录（cookie、进度、输出）
DATA_DIR: str = os.path.join(BASE_DIR, "data")

# cookie 持久化文件
COOKIE_FILE: str = os.path.join(DATA_DIR, "cookies.json")

# 默认抓取时间范围（天）
DEFAULT_DAYS: int = 30

# 请求间隔（秒）：越小越快但越容易被风控
# speed 1 = 慢(1.2s), 2 = 中(0.6s), 3 = 快(0.25s)
SPEED_MAP: dict[int, float] = {1: 1.2, 2: 0.6, 3: 0.25}

# 抓取速度 → 并发请求线程数。
# 平均 QPS 仍由 SPEED_MAP 决定，这里只决定「同一时刻最多几个请求在途」，
# 用来把网络等待时间重叠掉（I/O 密集，并发不增加请求总数）。
# 慢速保持严格串行（最稳）；中/快适度并发以缩短总耗时。
# 想更保守可以整体调小，例如 {1: 1, 2: 1, 3: 2}。
SPEED_WORKERS: dict[int, int] = {1: 1, 2: 2, 3: 3}

# 每次分页拉取的条数（评论接口）
COMMENT_PAGE_SIZE: int = 50
# 每页微博数量
WEIBO_PAGE_SIZE: int = 20

# 网络请求默认头
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 默认输出目录
OUTPUT_DIR: str = os.path.join(DATA_DIR, "output")
